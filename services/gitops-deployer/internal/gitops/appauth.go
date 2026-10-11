package gitops

import (
	"context"
	"crypto/rsa"
	"crypto/x509"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"net/http"
	"os"
	"strconv"
	"sync"
	"time"

	"github.com/go-jose/go-jose/v4"
	"github.com/go-jose/go-jose/v4/jwt"
)

// AppToken は GitHub App の installation access token を出す。App の秘密鍵で署名した JWT (10 分以内) で
// POST /app/installations/<id>/access_tokens を打ち、返るトークン (1 時間) を切れる 5 分前まで使い回す。
// トークンは home-k8s 1 つと contents・pull_requests の書き込みだけに絞って取る (installation がほかの
// リポジトリや権限を持っていても、この Pod が使えるのはそこまで)。秘密鍵は取り直すたびにファイルから読む
// (Secret を差し替えれば Pod を作り直さずに次の取り直しから効く)。
type AppToken struct {
	API            string // https://api.github.com
	AppID          string
	InstallationID string
	KeyFile        string
	Repo           string
	HTTP           *http.Client
	Now            func() time.Time

	mu      sync.Mutex
	token   string
	expires time.Time
}

// Token は使えるトークンを返す。切れる 5 分前を過ぎていれば取り直す。
func (a *AppToken) Token(ctx context.Context) (string, error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	now := a.now()
	if a.token != "" && now.Before(a.expires.Add(-5*time.Minute)) {
		return a.token, nil
	}
	tok, exp, err := a.fetch(ctx, now)
	if err != nil {
		return "", err
	}
	a.token, a.expires = tok, exp
	return tok, nil
}

// Invalidate は持っているトークンを捨てる (GitHub が 401 を返したとき。次の Token で取り直す)。
func (a *AppToken) Invalidate() {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.token = ""
}

func (a *AppToken) now() time.Time {
	if a.Now != nil {
		return a.Now()
	}
	return time.Now()
}

func (a *AppToken) fetch(ctx context.Context, now time.Time) (string, time.Time, error) {
	signed, err := a.appJWT(now)
	if err != nil {
		return "", time.Time{}, err
	}
	body := map[string]any{
		"repositories": []string{a.Repo},
		"permissions":  map[string]string{"contents": "write", "pull_requests": "write"},
	}
	var out struct {
		Token     string    `json:"token"`
		ExpiresAt time.Time `json:"expires_at"`
	}
	gh := &GitHub{HTTP: a.HTTP}
	err = gh.do(ctx, http.MethodPost, a.API+"/app/installations/"+a.InstallationID+"/access_tokens", "Bearer "+signed, body, &out)
	if err != nil {
		return "", time.Time{}, fmt.Errorf("installation token: %w", err)
	}
	if out.Token == "" {
		return "", time.Time{}, errors.New("installation token: token が空")
	}
	return out.Token, out.ExpiresAt, nil
}

// appJWT は App として名乗る JWT (RS256、iss は App ID)。iat は時計のずれを見込んで 60 秒戻し、exp は上限の 10 分より短い 9 分。
func (a *AppToken) appJWT(now time.Time) (string, error) {
	key, err := readRSAKey(a.KeyFile)
	if err != nil {
		return "", err
	}
	if _, err := strconv.ParseInt(a.AppID, 10, 64); err != nil {
		return "", fmt.Errorf("GitHub App の ID が数でない: %q", a.AppID)
	}
	signer, err := jose.NewSigner(jose.SigningKey{Algorithm: jose.RS256, Key: key}, (&jose.SignerOptions{}).WithType("JWT"))
	if err != nil {
		return "", err
	}
	return jwt.Signed(signer).Claims(jwt.Claims{
		Issuer:   a.AppID,
		IssuedAt: jwt.NewNumericDate(now.Add(-60 * time.Second)),
		Expiry:   jwt.NewNumericDate(now.Add(9 * time.Minute)),
	}).Serialize()
}

// readRSAKey は GitHub が配る PEM (PKCS#1 の RSA PRIVATE KEY。PKCS#8 も読む)。
func readRSAKey(path string) (*rsa.PrivateKey, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("GitHub App の秘密鍵: %w", err)
	}
	block, _ := pem.Decode(b)
	if block == nil {
		return nil, fmt.Errorf("GitHub App の秘密鍵 %s が PEM でない", path)
	}
	if k, err := x509.ParsePKCS1PrivateKey(block.Bytes); err == nil {
		return k, nil
	}
	k, err := x509.ParsePKCS8PrivateKey(block.Bytes)
	if err != nil {
		return nil, fmt.Errorf("GitHub App の秘密鍵 %s が読めない: %w", path, err)
	}
	rk, ok := k.(*rsa.PrivateKey)
	if !ok {
		return nil, fmt.Errorf("GitHub App の秘密鍵 %s が RSA でない", path)
	}
	return rk, nil
}

// decodeJSON は応答の本文を out に読む (out が nil なら捨てる)。
func decodeJSON(b []byte, out any) error {
	if out == nil || len(b) == 0 {
		return nil
	}
	return json.Unmarshal(b, out)
}
