package http

import (
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestSNSPartial200ContinuesWithVerifiedRange(t *testing.T) {
	calls := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		if calls == 1 {
			w.Header().Set("Content-Range", "bytes 0-2/6")
			fmt.Fprint(w, "abc")
			return
		}
		if r.Header.Get("Range") != "bytes=3-8388610" {
			t.Errorf("unexpected Range %q", r.Header.Get("Range"))
		}
		w.Header().Set("Content-Range", "bytes 3-5/6")
		w.WriteHeader(206)
		fmt.Fprint(w, "def")
	}))
	defer server.Close()
	data, _, err := downloadSNSOriginal(context.Background(), server.Client(), server.URL)
	if err != nil || string(data) != "abcdef" || calls != 2 {
		t.Fatalf("data=%q calls=%d error=%v", data, calls, err)
	}
}

func TestSNSIncompleteOrInconsistentRangeIsRejected(t *testing.T) {
	for _, header := range []string{"bytes 1-3/6", "bytes 0-4/6", "bytes 0-2/536870913", "garbage"} {
		t.Run(header, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.Header().Set("Content-Range", header)
				fmt.Fprint(w, "abc")
			}))
			defer server.Close()
			if _, _, err := downloadSNSOriginal(context.Background(), server.Client(), server.URL); err == nil {
				t.Fatal("accepted invalid partial response")
			}
		})
	}
}

func TestSNSCompleteAndEmptyResponse(t *testing.T) {
	for _, body := range []string{"complete", ""} {
		server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, body) }))
		data, _, err := downloadSNSOriginal(context.Background(), server.Client(), server.URL)
		server.Close()
		if body == "" && err == nil {
			t.Fatal("accepted empty response")
		}
		if body != "" && (err != nil || string(data) != body) {
			t.Fatalf("%q %v", data, err)
		}
	}
}

func TestSNSRequestHonorsCancellation(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { <-r.Context().Done() }))
	defer server.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Millisecond)
	defer cancel()
	if _, _, err := downloadSNSOriginal(ctx, server.Client(), server.URL); err == nil {
		t.Fatal("ignored cancellation")
	}
}

func TestSNSCacheAtomicallyReplacesBytes(t *testing.T) {
	path := filepath.Join(t.TempDir(), "cached.bin")
	if err := os.WriteFile(path, []byte("old"), 0644); err != nil {
		t.Fatal(err)
	}
	if err := storeSNSCache(path, []byte("new")); err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile(path)
	if err != nil || string(data) != "new" {
		t.Fatalf("%q %v", data, err)
	}
	matches, _ := filepath.Glob(filepath.Join(filepath.Dir(path), ".sns-media-*"))
	if len(matches) > 0 {
		t.Fatal("left temporary cache files")
	}
}
