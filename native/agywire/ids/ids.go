package ids

import (
	"crypto/rand"
	"fmt"
	"strconv"
)

func SessionID(stable string) string {
	h := uint64(0xcbf29ce484222325)
	for i := 0; i < len(stable); i++ {
		h ^= uint64(stable[i])
		h *= 0x100000001b3
	}
	return strconv.FormatInt(int64(h), 10)
}

var AgentSessionID = SessionID("")

func UUID() string {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		panic(err)
	}
	b[6] = (b[6] & 0x0f) | 0x40
	b[8] = (b[8] & 0x3f) | 0x80
	return fmt.Sprintf("%x-%x-%x-%x-%x", b[0:4], b[4:6], b[6:8], b[8:10], b[10:16])
}

func AgentRequestID(cascade string, unixMilli int64, traj string, n int) string {
	return fmt.Sprintf("agent/%s/%d/%s/%d", cascade, unixMilli, traj, n)
}

func PrefixedRequestID(prefix string) string {
	return prefix + "/" + UUID()
}
