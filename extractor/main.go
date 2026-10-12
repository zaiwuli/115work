// A single-task bridge to UnpackFlow's vendored extractor.
package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"

	"golift.io/xtractr"
)

type request struct {
	Mode             string `json:"mode"`
	Source           string `json:"source"`
	Output           string `json:"output"`
	Password         string `json:"password"`
	MaxBytes         int64  `json:"max_bytes"`
	MaxFiles         int    `json:"max_files"`
	DisableRecursion bool   `json:"disable_recursion"`
	RecurseISO       bool   `json:"recurse_iso"`
	SquashRoot       bool   `json:"squash_root"`
	ExcludeISO       bool   `json:"exclude_iso"`
}

type budget struct {
	output            string
	maxBytes, written int64
	maxFiles          int
	seen              map[string]bool
	directories       map[string]bool
}

func (b *budget) validate(path string, mode os.FileMode) error {
	relative, err := filepath.Rel(b.output, path)
	if err != nil || relative == ".." || strings.HasPrefix(relative, ".."+string(filepath.Separator)) {
		return errors.New("ARCHIVE_PATH_REJECTED")
	}
	if mode&os.ModeType != 0 && !mode.IsDir() {
		return errors.New("ARCHIVE_SPECIAL_FILE_REJECTED")
	}
	if mode.IsDir() {
		key := strings.ToLower(relative)
		if relative != "." && !b.directories[key] {
			if len(b.directories)+len(b.seen) >= b.maxFiles {
				return errors.New("ARCHIVE_EXPANSION_LIMIT")
			}
			b.directories[key] = true
		}
		return nil
	}
	if _, err := os.Lstat(path); err == nil {
		return errors.New("DUPLICATE_ARCHIVE_ENTRY")
	}
	key := strings.ToLower(relative)
	if b.seen[key] {
		return errors.New("DUPLICATE_ARCHIVE_ENTRY")
	}
	if len(b.seen)+len(b.directories) >= b.maxFiles {
		return errors.New("ARCHIVE_EXPANSION_LIMIT")
	}
	b.seen[key] = true
	return nil
}

type budgetWriter struct {
	io.Writer
	b *budget
}

func (w *budgetWriter) Write(data []byte) (int, error) {
	if int64(len(data)) > w.b.maxBytes-w.b.written {
		return 0, errors.New("ARCHIVE_EXPANSION_LIMIT")
	}
	n, err := w.Writer.Write(data)
	w.b.written += int64(n)
	return n, err
}

func safeName(name string) string {
	name = strings.ReplaceAll(name, "\\", "/")
	if strings.HasPrefix(name, "/") || strings.ContainsAny(name, ":\x00") {
		panic("ARCHIVE_PATH_REJECTED")
	}
	parts := strings.Split(name, "/")
	if len(parts) > 25 {
		panic("ARCHIVE_DEPTH_LIMIT")
	}
	for _, part := range parts {
		if part == ".." || strings.HasSuffix(part, " ") || strings.HasSuffix(part, ".") && part != "." {
			panic("ARCHIVE_PATH_REJECTED")
		}
		base := strings.ToUpper(strings.SplitN(part, ".", 2)[0])
		if base == "CON" || base == "PRN" || base == "AUX" || base == "NUL" ||
			len(base) == 4 && (strings.HasPrefix(base, "COM") || strings.HasPrefix(base, "LPT")) &&
				base[3] >= '1' && base[3] <= '9' {
			panic("ARCHIVE_PATH_REJECTED")
		}
	}
	return name
}

func run(r request) (map[string]any, error) {
	if r.MaxBytes <= 0 || r.MaxFiles <= 0 {
		return nil, errors.New("INVALID_EXTRACTION_LIMIT")
	}
	output, err := filepath.Abs(r.Output)
	if err != nil {
		return nil, err
	}
	b := &budget{output: output, maxBytes: r.MaxBytes, maxFiles: r.MaxFiles,
		seen: map[string]bool{}, directories: map[string]bool{}}
	mapper := newArchiveNameMapper()
	file := &xtractr.XFile{
		FilePath: r.Source, OutputDir: output, Password: r.Password,
		FileMode: 0o600, DirMode: 0o700, FileWorkers: 1,
		NameMapper: func(name string) string { return mapper.Map(safeName(name)) }, ValidateOutput: b.validate,
		WrapOutput: func(writer io.Writer) io.Writer { return &budgetWriter{writer, b} },
		SquashRoot: r.SquashRoot, SquashRootName: true,
	}
	_, _, _, err = file.Extract()
	if err == nil && !r.DisableRecursion && (r.RecurseISO || !strings.HasSuffix(strings.ToLower(r.Source), ".iso")) {
		skip := map[string]bool{}
		for _, path := range file.SkipOnRecursion {
			skip[filepath.Clean(path)] = true
		}
		excluded := xtractr.Exclude{}
		if r.ExcludeISO {
			excluded = append(excluded, ".iso")
		}
		for _, archives := range xtractr.FindCompressedFiles(xtractr.Filter{
			Path: output, MaxDepth: 25, ExcludeSuffix: excluded,
		}) {
			for _, archive := range archives {
				if skip[filepath.Clean(archive)] {
					continue
				}
				nested := *file
				nested.FilePath = archive
				nested.Passwords = nil
				_, _, _, err = nested.Extract()
				if err != nil {
					break
				}
			}
			if err != nil {
				break
			}
		}
	}
	if err != nil {
		for _, code := range []string{"ARCHIVE_PATH_REJECTED", "ARCHIVE_SPECIAL_FILE_REJECTED",
			"DUPLICATE_ARCHIVE_ENTRY", "ARCHIVE_EXPANSION_LIMIT"} {
			if strings.Contains(err.Error(), code) {
				return nil, errors.New(code)
			}
		}
		return nil, errors.New("PASSWORD_OR_MISSING_PARTS_OR_CORRUPT_ARCHIVE")
	}
	files := []map[string]any{}
	var total int64
	err = filepath.WalkDir(output, func(path string, entry os.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		if entry.IsDir() {
			return nil
		}
		info, err := entry.Info()
		if err != nil {
			return err
		}
		if !info.Mode().IsRegular() {
			return errors.New("ARCHIVE_SPECIAL_FILE_REJECTED")
		}
		total += info.Size()
		if total > r.MaxBytes || len(files) >= r.MaxFiles {
			return errors.New("ARCHIVE_EXPANSION_LIMIT")
		}
		reader, err := os.Open(path)
		if err != nil {
			return err
		}
		hash := sha256.New()
		_, err = io.Copy(hash, reader)
		reader.Close()
		if err != nil {
			return err
		}
		relative, _ := filepath.Rel(output, path)
		files = append(files, map[string]any{"path": filepath.ToSlash(relative), "size": info.Size(),
			"sha256": hex.EncodeToString(hash.Sum(nil))})
		return nil
	})
	if err != nil {
		return nil, err
	}
	if len(files) == 0 {
		return nil, errors.New("ARCHIVE_HAS_NO_FILES")
	}
	return map[string]any{"files": files, "total_bytes": total, "engine": "xtractr",
		"name_mapping": mapper.Manifest()}, nil
}

func main() {
	defer func() {
		if recover() != nil {
			fmt.Println(`{"ok":false,"error":"ARCHIVE_PATH_OR_RESOURCE_REJECTED"}`)
			os.Exit(1)
		}
	}()
	var r request
	if err := json.NewDecoder(io.LimitReader(os.Stdin, 65536)).Decode(&r); err != nil {
		fmt.Println(`{"ok":false,"error":"INVALID_EXTRACTOR_REQUEST"}`)
		os.Exit(1)
	}
	if r.Mode == "inputs" {
		inputs, err := xtractr.CUEInputs(r.Source)
		if err != nil {
			code := "CUE_COMPANION_NOT_READY"
			if strings.Contains(err.Error(), "REJECTED") {
				code = "CUE_INPUT_REJECTED"
			}
			json.NewEncoder(os.Stdout).Encode(map[string]any{"ok": false, "error": code})
			os.Exit(1)
		}
		json.NewEncoder(os.Stdout).Encode(map[string]any{"ok": true, "inputs": inputs})
		return
	}
	result, err := run(r)
	if err != nil {
		json.NewEncoder(os.Stdout).Encode(map[string]any{"ok": false, "error": err.Error()})
		os.Exit(1)
	}
	json.NewEncoder(os.Stdout).Encode(map[string]any{"ok": true, "manifest": result})
}
