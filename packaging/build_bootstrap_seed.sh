#!/bin/sh
# Development host only. A consumer receives the independently authenticated seed.
#
#   build_bootstrap_seed.sh --output-directory NEW_DIR
#   build_bootstrap_seed.sh --output-directory NEW_DIR --sign-identity IDENTITY --team-id TEAMID
#
# Without --sign-identity: the ad hoc seed (kind developer-built-ad-hoc-seed), unchanged byte for byte.
# With it (kind developer-id-seed, publisher_identity true): `codesign --force --timestamp --options
# runtime --sign IDENTITY --identifier <CODESIGN_ID_PREFIX>.bootstrap-verifier`, the prefix read from
# src/telegram_search_mcp/product_identity.py; then the designated requirement with TEAMID (exactly
# native_runtime.designated_requirement), the identifier, the Team ID, the Developer ID leaf, a secure
# timestamp and the hardened-runtime flag are checked. IDENTITY is "Developer ID Application: NAME
# (TEAMID)" or the certificate's SHA-1; codesign reaches the login Keychain through HOME, and no secret
# is passed. Every refusal is one fixed code.
set -eu
umask 077
PATH=/usr/bin:/bin:/usr/sbin:/sbin
export PATH
unset CDPATH ENV BASH_ENV DYLD_INSERT_LIBRARIES DYLD_LIBRARY_PATH DYLD_FRAMEWORK_PATH
# Tools by absolute path only. Tests rewrite exactly this block in a copy of the script.
tool_codesign=/usr/bin/codesign
# End of tools.
fail() { /usr/bin/printf '%s\n' '{"schema_version":1,"error_code":"bootstrap_seed_build_failed"}' >&2; exit 1; }
identity='' team=''
case "$#" in
  2) [ "$1" = --output-directory ] || fail ;;
  6) [ "$1" = --output-directory ] && [ "$3" = --sign-identity ] && [ "$5" = --team-id ] || fail
     identity=$4 team=$6 ;;
  *) fail ;;
esac
output=$2
if [ "$#" -eq 6 ]; then
  [ "${#team}" -eq 10 ] || fail
  case "$team" in *[!0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ]*) fail ;; esac
  [ "${#identity}" -ge 1 ] && [ "${#identity}" -le 200 ] || fail
  case "$identity" in *[!A-Za-z0-9\ .,:\'\(\)_-]*) fail ;; esac
  case "$identity" in
    "Developer ID Application: "?*" ($team)") ;;
    *) [ "${#identity}" -eq 40 ] || fail
       case "$identity" in *[!0123456789ABCDEF]*) fail ;; esac ;;
  esac
  case "${HOME:-}" in /*) ;; *) fail ;; esac
fi
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
[ -x /usr/bin/clang ] && [ -x "$tool_codesign" ] && [ -x /usr/bin/otool ] || fail
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
signing_id=''
if [ -n "$identity" ]; then
  # The single CODESIGN_ID_PREFIX line of product_identity.py, under product_identity's own grammar.
  identity_file=$(/usr/bin/dirname "$source_dir")/src/telegram_search_mcp/product_identity.py
  [ -f "$identity_file" ] && [ ! -L "$identity_file" ] || fail
  prefix_lines=$(/usr/bin/env -i LC_ALL=C /usr/bin/grep -c '^CODESIGN_ID_PREFIX' "$identity_file") || fail
  [ "$prefix_lines" = 1 ] || fail
  prefix=$(/usr/bin/env -i LC_ALL=C /usr/bin/sed -n "s/^CODESIGN_ID_PREFIX: Final = '\([a-z0-9.-]*\)'\$/\1/p" "$identity_file") || fail
  [ "${#prefix}" -ge 5 ] && [ "${#prefix}" -le 100 ] || fail
  case "$prefix" in [!abcdefghijklmnopqrstuvwxyz]*|*[!abcdefghijklmnopqrstuvwxyz0123456789.-]*) fail ;; esac
  case "$prefix" in *..*|*.|*-|*.-*|*-.*) fail ;; esac
  case "$prefix" in *.*.*) ;; *) fail ;; esac
  case "${prefix%%.*}" in *-*) fail ;; esac
  signing_id=$prefix.bootstrap-verifier
fi
/bin/mkdir -m 700 "$output" || fail
/usr/bin/env -i PATH=/usr/bin:/bin:/usr/sbin:/sbin LC_ALL=C /usr/bin/clang -target arm64-apple-macos26.0 -std=c11 -O2 -Wall -Wextra -Werror \
  -Wno-deprecated-declarations -fstack-protector-strong \
  "$source_file" -lz -o "$output/bootstrap-verifier" \
  >"$output/compiler.log" 2>&1 || fail
if [ -z "$identity" ]; then
  /usr/bin/env -i PATH=/usr/bin:/bin LC_ALL=C "$tool_codesign" --force --sign - --options runtime "$output/bootstrap-verifier" \
    >"$output/codesign.log" 2>&1 || fail
  /usr/bin/env -i PATH=/usr/bin:/bin LC_ALL=C "$tool_codesign" --verify --strict "$output/bootstrap-verifier" \
    >>"$output/codesign.log" 2>&1 || fail
else
  /usr/bin/env -i PATH=/usr/bin:/bin LC_ALL=C HOME="$HOME" "$tool_codesign" --force --timestamp --options runtime \
    --sign "$identity" --identifier "$signing_id" "$output/bootstrap-verifier" >"$output/codesign.log" 2>&1 || fail
  /usr/bin/env -i PATH=/usr/bin:/bin LC_ALL=C "$tool_codesign" --verify --strict -R "=anchor apple generic and certificate 1[field.1.2.840.113635.100.6.2.6] exists and certificate leaf[field.1.2.840.113635.100.6.1.13] exists and certificate leaf[subject.OU] = \"$team\"" \
    "$output/bootstrap-verifier" >>"$output/codesign.log" 2>&1 || fail
  /usr/bin/env -i PATH=/usr/bin:/bin LC_ALL=C "$tool_codesign" -d --verbose=4 "$output/bootstrap-verifier" \
    >"$output/signature.txt" 2>&1 || fail
  # A closed reading of what codesign shows: one identifier, one Team ID, the Developer ID leaf first,
  # a secure timestamp (never the unauthenticated `Signed Time`) and the hardened runtime as the only flag.
  signature_count() { /usr/bin/env -i LC_ALL=C /usr/bin/grep -c "$@" "$output/signature.txt" || :; }
  [ "$(signature_count -Fx "Identifier=$signing_id")" = 1 ] || fail
  [ "$(signature_count -Fx "TeamIdentifier=$team")" = 1 ] || fail
  [ "$(signature_count -E '^Timestamp=.')" = 1 ] && [ "$(signature_count -E '^Signed Time=')" = 0 ] || fail
  [ "$(signature_count -E '^CodeDirectory v=[0-9a-f]+ size=[0-9]+ flags=0x10000\(runtime\) ')" = 1 ] || fail
  first_authority=$(/usr/bin/env -i LC_ALL=C /usr/bin/sed -n '/^Authority=/{p;q;}' "$output/signature.txt") || fail
  case "$first_authority" in "Authority=Developer ID Application: "?*" ($team)") ;; *) fail ;; esac
fi
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
if [ -z "$identity" ]; then
  /usr/bin/printf '{"schema_version":1,"kind":"developer-built-ad-hoc-seed","target":"macos-arm64","minimum_macos":"26.0","source_commit":"%s","source_sha256":"%s","build_script_sha256":"%s","binary_sha256":"%s","binary_bytes":%s,"compiler":"/usr/bin/clang","publisher_identity":false}\n' \
    "$commit" "$source_sha" "$script_sha" "$binary_sha" "$binary_bytes" >"$output/provenance.json"
  /bin/chmod 400 "$output"/*.txt "$output"/*.log "$output/provenance.json"
  /usr/bin/printf '%s\n' '{"schema_version":1,"built":true,"target":"macos-arm64","minimum_macos":"26.0","publisher_identity":false}'
else
  /usr/bin/printf '{"schema_version":1,"kind":"developer-id-seed","target":"macos-arm64","minimum_macos":"26.0","source_commit":"%s","source_sha256":"%s","build_script_sha256":"%s","binary_sha256":"%s","binary_bytes":%s,"compiler":"/usr/bin/clang","publisher_identity":true,"team_id":"%s","identifier":"%s","hardened_runtime":true,"secure_timestamp":true}\n' \
    "$commit" "$source_sha" "$script_sha" "$binary_sha" "$binary_bytes" "$team" "$signing_id" >"$output/provenance.json"
  /bin/chmod 400 "$output"/*.txt "$output"/*.log "$output/provenance.json"
  /usr/bin/printf '{"schema_version":1,"built":true,"target":"macos-arm64","minimum_macos":"26.0","publisher_identity":true,"team_id":"%s"}\n' "$team"
fi
