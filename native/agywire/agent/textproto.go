package agent

import (
	"errors"
	"strconv"
	"strings"
	"unicode/utf8"
)

type tpNode struct {
	fields map[string][]any
	order  []string
}

func (n *tpNode) add(k string, v any) {
	if n.fields == nil {
		n.fields = map[string][]any{}
	}
	if _, ok := n.fields[k]; !ok {
		n.order = append(n.order, k)
	}
	n.fields[k] = append(n.fields[k], v)
}

func (n *tpNode) str(k string) string {
	if n == nil {
		return ""
	}
	for _, v := range n.fields[k] {
		if s, ok := v.(string); ok {
			return s
		}
	}
	return ""
}

func (n *tpNode) strs(k string) []string {
	if n == nil {
		return nil
	}
	var out []string
	for _, v := range n.fields[k] {
		if s, ok := v.(string); ok {
			out = append(out, s)
		}
	}
	return out
}

func (n *tpNode) msg(k string) *tpNode {
	if n == nil {
		return nil
	}
	for _, v := range n.fields[k] {
		if m, ok := v.(*tpNode); ok {
			return m
		}
	}
	return nil
}

func (n *tpNode) msgs(k string) []*tpNode {
	if n == nil {
		return nil
	}
	var out []*tpNode
	for _, v := range n.fields[k] {
		if m, ok := v.(*tpNode); ok {
			out = append(out, m)
		}
	}
	return out
}

func (n *tpNode) num(k string) float64 {
	f, _ := strconv.ParseFloat(n.str(k), 64)
	return f
}

type tpLexer struct {
	s   string
	pos int
}

func (l *tpLexer) skip() {
	for l.pos < len(l.s) {
		c := l.s[l.pos]
		switch {
		case c == '#':
			for l.pos < len(l.s) && l.s[l.pos] != '\n' {
				l.pos++
			}
		case c == ' ' || c == '\t' || c == '\n' || c == '\r' || c == ',' || c == ';':
			l.pos++
		default:
			return
		}
	}
}

func parseTextproto(s string) (*tpNode, error) {
	l := &tpLexer{s: s}
	n, err := l.fields(false)
	if err != nil {
		return nil, err
	}
	return n, nil
}

func (l *tpLexer) fields(nested bool) (*tpNode, error) {
	n := &tpNode{}
	for {
		l.skip()
		if l.pos >= len(l.s) {
			if nested {
				return nil, errors.New("textproto: 缺少 }")
			}
			return n, nil
		}
		if l.s[l.pos] == '}' {
			if !nested {
				return nil, errors.New("textproto: 多余的 }")
			}
			l.pos++
			return n, nil
		}
		name := l.ident()
		if name == "" {
			return nil, errors.New("textproto: 期望字段名 @" + strconv.Itoa(l.pos))
		}
		l.skip()
		if l.pos < len(l.s) && l.s[l.pos] == ':' {
			l.pos++
			l.skip()
		}
		if l.pos < len(l.s) && (l.s[l.pos] == '{' || l.s[l.pos] == '<') {
			l.pos++
			child, err := l.fields(true)
			if err != nil {
				return nil, err
			}
			n.add(name, child)
			continue
		}
		v, err := l.scalar()
		if err != nil {
			return nil, err
		}
		n.add(name, v)
	}
}

func (l *tpLexer) ident() string {
	st := l.pos
	for l.pos < len(l.s) {
		c := l.s[l.pos]
		if c == '_' || c == '.' || c == '-' || c == '+' || (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') {
			l.pos++
			continue
		}
		break
	}
	return l.s[st:l.pos]
}

func (l *tpLexer) scalar() (string, error) {
	if l.pos < len(l.s) && (l.s[l.pos] == '"' || l.s[l.pos] == '\'') {
		var b strings.Builder
		for l.pos < len(l.s) && (l.s[l.pos] == '"' || l.s[l.pos] == '\'') {
			s, err := l.quoted()
			if err != nil {
				return "", err
			}
			b.WriteString(s)
			l.skip()
		}
		return b.String(), nil
	}
	v := l.ident()
	if v == "" {
		return "", errors.New("textproto: 期望值 @" + strconv.Itoa(l.pos))
	}
	return v, nil
}

func (l *tpLexer) quoted() (string, error) {
	q := l.s[l.pos]
	l.pos++
	var b []byte
	for l.pos < len(l.s) {
		c := l.s[l.pos]
		if c == q {
			l.pos++
			return string(b), nil
		}
		if c == '\n' {
			return "", errors.New("textproto: 字符串内换行")
		}
		if c != '\\' {
			b = append(b, c)
			l.pos++
			continue
		}
		l.pos++
		if l.pos >= len(l.s) {
			break
		}
		e := l.s[l.pos]
		l.pos++
		switch e {
		case 'n':
			b = append(b, '\n')
		case 't':
			b = append(b, '\t')
		case 'r':
			b = append(b, '\r')
		case 'a':
			b = append(b, 7)
		case 'b':
			b = append(b, 8)
		case 'f':
			b = append(b, 12)
		case 'v':
			b = append(b, 11)
		case '\\', '"', '\'', '?':
			b = append(b, e)
		case 'x', 'X':
			v, n := 0, 0
			for n < 2 && l.pos < len(l.s) && isHex(l.s[l.pos]) {
				v = v*16 + hexVal(l.s[l.pos])
				l.pos++
				n++
			}
			b = append(b, byte(v))
		case 'u', 'U':
			width := 4
			if e == 'U' {
				width = 8
			}
			if l.pos+width > len(l.s) {
				return "", errors.New("textproto: \\u 截断")
			}
			r, err := strconv.ParseUint(l.s[l.pos:l.pos+width], 16, 32)
			if err != nil {
				return "", err
			}
			l.pos += width
			b = utf8.AppendRune(b, rune(r))
		default:
			if e >= '0' && e <= '7' {
				v := int(e - '0')
				for n := 1; n < 3 && l.pos < len(l.s) && l.s[l.pos] >= '0' && l.s[l.pos] <= '7'; n++ {
					v = v*8 + int(l.s[l.pos]-'0')
					l.pos++
				}
				b = append(b, byte(v))
			} else {
				return "", errors.New("textproto: 未知转义 \\" + string(e))
			}
		}
	}
	return "", errors.New("textproto: 字符串未闭合")
}

func isHex(c byte) bool {
	return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F')
}

func hexVal(c byte) int {
	switch {
	case c >= '0' && c <= '9':
		return int(c - '0')
	case c >= 'a' && c <= 'f':
		return int(c-'a') + 10
	default:
		return int(c-'A') + 10
	}
}
