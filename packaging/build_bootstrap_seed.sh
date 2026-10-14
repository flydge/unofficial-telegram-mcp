#!/bin/sh
# Development host only. A consumer receives the independently authenticated seed.
set -eu
umask 077
PATH=/usr/bin:/bin:/usr/sbin:/sbin
export PATH
unset CDPATH ENV BASH_ENV DYLD_INSERT_LIBRARIES DYLD_LIBRARY_PATH DYLD_FRAMEWORK_PATH
fail() { /usr/bin/printf '%s\n' '{"schema_version":1,"error_code":"bootstrap_seed_build_failed"}' >&2; exit 1; }
[ "$#" -eq 2 ] && [ "$1" = --output-directory ] || fail
output=$2
case "$output" in /*) ;; *) fail ;; esac
case "$output" in *'/../'*|*'/./'*|*'//'*|*/..|*/.|*/) fail ;; esac
[ ! -e "$output" ] && [ ! -L "$output" ] || fail
source_dir=$(/usr/bin/dirname "$0")
source_dir=$(cd -P "$source_dir" && /bin/pwd -P)
source_file=$source_dir/bootstrap_verifier.c
[ -f "$source_file" ] && [ ! -L "$source_file" ] || fail
output_parent=$(/usr/bin/dirname "$output")
[ -d "$output_parent" ] && [ ! -L "$output_parent" ] || fail
canonical_parent=$(cd -P "$output_parent" && /bin/pwd -P)
[ "$canonical_parent" = "$output_parent" ] || fail
probe=$output_parent
while [ "$probe" != / ]; do
  [ ! -d "$probe/.git" ] && [ ! -f "$probe/.git" ] || fail
  probe=$(/usr/bin/dirname "$probe")
done
[ -x /usr/bin/clang ] && [ -x /usr/bin/codesign ] && [ -x /usr/bin/otool ] || fail
[ "$(/usr/bin/uname -m)" = arm64 ] || fail
digest() {
  digest_record=$(/usr/bin/env -i PATH=/usr/bin:/bin /usr/bin/openssl dgst -sha256 "$1") || return 1
  digest_value=$(/usr/bin/printf '%s\n' "$digest_record" | /usr/bin/awk '{print $NF}')
  [ "${#digest_value}" -eq 64 ] || return 1
  case "$digest_value" in *[!0-9a-f]*) return 1 ;; esac
  /usr/bin/printf '%s\n' "$digest_value"
}
source_sha=$(digest "$source_file")
script_sha=$(digest "$source_dir/build_bootstrap_seed.sh")
commit=$(/usr/bin/env -i PATH=/usr/bin:/bin /usr/bin/git -C "$source_dir" rev-parse HEAD 2>/dev/null) || fail
case "$commit" in *[!0-9a-f]*|'') fail ;; esac
/bin/mkdir -m 700 "$output" || fail
/usr/bin/env -i PATH=/usr/bin:/bin:/usr/sbin:/sbin LC_ALL=C /usr/bin/clang -target arm64-apple-macos26.0 -std=c11 -O2 -Wall -Wextra -Werror \
  -Wno-deprecated-declarations -fstack-protector-strong \
  "$source_file" -lz -o "$output/bootstrap-verifier" \
  >"$output/compiler.log" 2>&1 || fail
/usr/bin/env -i PATH=/usr/bin:/bin LC_ALL=C /usr/bin/codesign --force --sign - --options runtime "$output/bootstrap-verifier" \
  >"$output/codesign.log" 2>&1 || fail
/usr/bin/env -i PATH=/usr/bin:/bin LC_ALL=C /usr/bin/codesign --verify --strict "$output/bootstrap-verifier" \
  >>"$output/codesign.log" 2>&1 || fail
/usr/bin/env -i PATH=/usr/bin:/bin LC_ALL=C /usr/bin/otool -L "$output/bootstrap-verifier" >"$output/libraries.txt" || fail
/usr/bin/awk 'NR>1 {if ($1 != "/usr/lib/libSystem.B.dylib" && $1 != "/usr/lib/libz.1.dylib") exit 1; count++} END {if (count != 2) exit 1}' \
  "$output/libraries.txt" || fail
/usr/bin/env -i PATH=/usr/bin:/bin LC_ALL=C /usr/bin/otool -l "$output/bootstrap-verifier" >"$output/load-commands.txt" || fail
/usr/bin/file "$output/bootstrap-verifier" >"$output/file.txt" || fail
/usr/bin/env -i PATH=/usr/bin:/bin LC_ALL=C /usr/bin/clang --version >"$output/compiler-version.txt" || fail
/bin/chmod 500 "$output/bootstrap-verifier"
[ "$source_sha" = "$(digest "$source_file")" ] && [ "$script_sha" = "$(digest "$source_dir/build_bootstrap_seed.sh")" ] || fail
[ "$commit" = "$(/usr/bin/env -i PATH=/usr/bin:/bin /usr/bin/git -C "$source_dir" rev-parse HEAD 2>/dev/null)" ] || fail
binary_sha=$(digest "$output/bootstrap-verifier")
binary_bytes=$(/usr/bin/stat -f %z "$output/bootstrap-verifier")
/usr/bin/printf '{"schema_version":1,"kind":"developer-built-ad-hoc-seed","target":"macos-arm64","minimum_macos":"26.0","source_commit":"%s","source_sha256":"%s","build_script_sha256":"%s","binary_sha256":"%s","binary_bytes":%s,"compiler":"/usr/bin/clang","publisher_identity":false}\n' \
  "$commit" "$source_sha" "$script_sha" "$binary_sha" "$binary_bytes" >"$output/provenance.json"
/bin/chmod 400 "$output"/*.txt "$output"/*.log "$output/provenance.json"
/usr/bin/printf '%s\n' '{"schema_version":1,"built":true,"target":"macos-arm64","minimum_macos":"26.0","publisher_identity":false}'
