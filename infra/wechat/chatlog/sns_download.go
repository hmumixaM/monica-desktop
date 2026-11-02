package http

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"time"
)

const snsOriginalLimit = 512 * 1024 * 1024

// A deadline covers the complete object; a single CDN range has its own bound.
// Some WeChat CDNs return HTTP 200 plus Content-Range for only the first MiB.
func downloadSNSOriginal(ctx context.Context, client *http.Client, rawURL string) ([]byte, string, error) {
	ctx, cancel := context.WithTimeout(ctx, 15*time.Minute)
	defer cancel()
	var output bytes.Buffer
	var expected int64 = -1
	var contentType string
	for {
		offset := int64(output.Len())
		req, err := http.NewRequestWithContext(ctx, http.MethodGet, rawURL, nil)
		if err != nil {
			return nil, "", fmt.Errorf("invalid SNS media URL")
		}
		req.Header.Set("User-Agent", "MicroMessenger Client")
		req.Header.Set("Accept", "*/*")
		req.Header.Set("Range", fmt.Sprintf("bytes=%d-%d", offset, offset+8*1024*1024-1))
		resp, err := client.Do(req)
		if err != nil {
			return nil, "", fmt.Errorf("SNS request failed: %w", err)
		}
		if resp.StatusCode != http.StatusOK && resp.StatusCode != http.StatusPartialContent {
			resp.Body.Close()
			return nil, "", fmt.Errorf("SNS media HTTP status %d", resp.StatusCode)
		}
		remaining := int64(snsOriginalLimit) - offset
		chunk, readErr := io.ReadAll(io.LimitReader(resp.Body, remaining+1))
		resp.Body.Close()
		if readErr != nil {
			return nil, "", fmt.Errorf("SNS body read failed: %w", readErr)
		}
		if len(chunk) == 0 || int64(len(chunk)) > remaining {
			return nil, "", fmt.Errorf("empty or oversized SNS media")
		}
		if contentType == "" {
			contentType = resp.Header.Get("Content-Type")
		}
		contentRange := resp.Header.Get("Content-Range")
		if contentRange == "" {
			if offset != 0 || resp.StatusCode != http.StatusOK || (resp.ContentLength >= 0 && int64(len(chunk)) != resp.ContentLength) {
				return nil, "", fmt.Errorf("incomplete SNS media response")
			}
			return chunk, contentType, nil
		}
		var start, end, total int64
		if _, err := fmt.Sscanf(contentRange, "bytes %d-%d/%d", &start, &end, &total); err != nil ||
			start != offset || end < start || end-start+1 != int64(len(chunk)) || total <= end || total > snsOriginalLimit ||
			(expected >= 0 && total != expected) {
			return nil, "", fmt.Errorf("invalid SNS media range")
		}
		expected = total
		output.Write(chunk)
		if int64(output.Len()) == expected {
			return output.Bytes(), contentType, nil
		}
	}
}

func storeSNSCache(path string, data []byte) error {
	file, err := os.CreateTemp(filepath.Dir(path), ".sns-media-*")
	if err != nil {
		return err
	}
	temporary := file.Name()
	defer os.Remove(temporary)
	if _, err = file.Write(data); err == nil {
		err = file.Sync()
	}
	if closeErr := file.Close(); err == nil {
		err = closeErr
	}
	if err != nil {
		return err
	}
	if err = os.Chmod(temporary, 0644); err != nil {
		return err
	}
	return os.Rename(temporary, path)
}
