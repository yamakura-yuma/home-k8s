// Package problem は API のエラーの形 (RFC 9457 problem+json)。bff と api が同じ形で返す。
package problem

import (
	"github.com/gin-gonic/gin"
)

const ContentType = "application/problem+json"

type Problem struct {
	Type   string `json:"type"`
	Title  string `json:"title"`
	Status int    `json:"status"`
	Detail string `json:"detail,omitempty"`
	// 入力の誤り (spec.FieldError の並び)
	Errors any `json:"errors,omitempty"`
}

// Write は problem+json を返し、以降のハンドラを止める。
func Write(c *gin.Context, status int, title, detail string) {
	WriteErrors(c, status, title, detail, nil)
}

func WriteErrors(c *gin.Context, status int, title, detail string, errs any) {
	c.Header("Content-Type", ContentType)
	c.AbortWithStatusJSON(status, Problem{Type: "about:blank", Title: title, Status: status, Detail: detail, Errors: errs})
}
