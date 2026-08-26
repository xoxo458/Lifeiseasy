#!/bin/sh
# Fetch the libraries that get inlined into the single-file app.
# Run once, then: python3 web/build.py
set -e
cd "$(dirname "$0")"
mkdir -p lib && cd lib
npm pack pdfjs-dist@4.0.379 exceljs@4.4.0
tar xzf pdfjs-dist-4.0.379.tgz
tar xzf exceljs-4.4.0.tgz
echo "libraries ready in web/lib/package - now run: python3 web/build.py"
