package pj

import (
	"math"
	"sort"
	"strconv"
	"unicode/utf8"
)

type Buf struct {
	b        []byte
	nonEmpty []bool
	afterKey bool
}

func (w *Buf) Bytes() []byte { return w.b }

func (w *Buf) String() string { return string(w.b) }

func (w *Buf) sep() {
	if w.afterKey {
		w.afterKey = false
		return
	}
	n := len(w.nonEmpty)
	if n == 0 {
		return
	}
	if w.nonEmpty[n-1] {
		w.b = append(w.b, ',')
	}
	w.nonEmpty[n-1] = true
}

func (w *Buf) ObjStart() *Buf {
	w.sep()
	w.b = append(w.b, '{')
	w.nonEmpty = append(w.nonEmpty, false)
	return w
}

func (w *Buf) ObjEnd() *Buf {
	w.b = append(w.b, '}')
	w.nonEmpty = w.nonEmpty[:len(w.nonEmpty)-1]
	return w
}

func (w *Buf) ArrStart() *Buf {
	w.sep()
	w.b = append(w.b, '[')
	w.nonEmpty = append(w.nonEmpty, false)
	return w
}

func (w *Buf) ArrEnd() *Buf {
	w.b = append(w.b, ']')
	w.nonEmpty = w.nonEmpty[:len(w.nonEmpty)-1]
	return w
}

func (w *Buf) Key(k string) *Buf {
	w.sep()
	w.b = AppendString(w.b, k)
	w.b = append(w.b, ':')
	w.afterKey = true
	return w
}

func (w *Buf) Str(s string) *Buf {
	w.sep()
	w.b = AppendString(w.b, s)
	return w
}

func (w *Buf) Bool(v bool) *Buf {
	w.sep()
	w.b = strconv.AppendBool(w.b, v)
	return w
}

func (w *Buf) Int32(v int64) *Buf {
	w.sep()
	w.b = strconv.AppendInt(w.b, v, 10)
	return w
}

func (w *Buf) Int64(v int64) *Buf {
	w.sep()
	w.b = append(w.b, '"')
	w.b = strconv.AppendInt(w.b, v, 10)
	w.b = append(w.b, '"')
	return w
}

func (w *Buf) Double(v float64) *Buf {
	w.sep()
	w.b = AppendDouble(w.b, v)
	return w
}

func (w *Buf) Raw(raw []byte) *Buf {
	w.sep()
	w.b = append(w.b, raw...)
	return w
}

func SortedKeys[V any](m map[string]V) []string {
	ks := make([]string, 0, len(m))
	for k := range m {
		ks = append(ks, k)
	}
	sort.Strings(ks)
	return ks
}

func AppendString(out []byte, s string) []byte {
	out = append(out, '"')
	for i := 0; i < len(s); {
		r, n := utf8.DecodeRuneInString(s[i:])
		if r == utf8.RuneError && n == 1 {
			out = append(out, "�"...)
			i++
			continue
		}
		switch {
		case r == '"':
			out = append(out, '\\', '"')
		case r == '\\':
			out = append(out, '\\', '\\')
		case r < ' ':
			switch r {
			case '\b':
				out = append(out, '\\', 'b')
			case '\f':
				out = append(out, '\\', 'f')
			case '\n':
				out = append(out, '\\', 'n')
			case '\r':
				out = append(out, '\\', 'r')
			case '\t':
				out = append(out, '\\', 't')
			default:
				out = append(out, '\\', 'u', '0', '0')
				const hex = "0123456789abcdef"
				out = append(out, hex[r>>4], hex[r&0xf])
			}
		default:
			out = append(out, s[i:i+n]...)
		}
		i += n
	}
	return append(out, '"')
}

func AppendDouble(out []byte, n float64) []byte {
	switch {
	case math.IsNaN(n):
		return append(out, `"NaN"`...)
	case math.IsInf(n, 1):
		return append(out, `"Infinity"`...)
	case math.IsInf(n, -1):
		return append(out, `"-Infinity"`...)
	}
	fmtc := byte('f')
	if abs := math.Abs(n); abs != 0 && (abs < 1e-6 || abs >= 1e21) {
		fmtc = 'e'
	}
	out = strconv.AppendFloat(out, n, fmtc, -1, 64)
	if fmtc == 'e' {

		k := len(out)
		if k >= 4 && out[k-4] == 'e' && out[k-2] == '0' {
			out[k-2] = out[k-1]
			out = out[:k-1]
		}
	}
	return out
}
