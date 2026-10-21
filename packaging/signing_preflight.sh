#!/bin/sh -p
# Owner preflight for signing and notarization (CLIENTS-6). Development host only: the owner's Mac.
# Prints only nonsecret results: the Developer ID Application common name, its Team ID and whether
# the notarytool keychain profile answers. The profile name is the only credential reference; no
# password, API key or Apple ID is read, passed or printed, and tool output is never echoed.
#
#   signing_preflight.sh --profile PROFILE [--team-id TEAMID]
#
# Exit 0 prints {"schema_version":1,"identity":"...","team_id":"...","notary":"ok"}; a refusal prints
# one fixed code.
set -eu
umask 077
PATH=/usr/bin:/bin:/usr/sbin:/sbin
export PATH
unset CDPATH ENV BASH_ENV DYLD_INSERT_LIBRARIES DYLD_LIBRARY_PATH DYLD_FRAMEWORK_PATH
# Tools by absolute path only. Tests rewrite exactly this block in a copy of the script.
tool_security=/usr/bin/security
tool_xcrun=/usr/bin/xcrun
# End of tools.

preflight_fail() {
    /usr/bin/printf '{"schema_version":1,"error_code":"%s"}\n' "$1" >&2
    exit "$2"
}
preflight_profile='' preflight_team='' preflight_seen=' '
while [ "$#" -gt 0 ]; do
    case "$preflight_seen" in *" $1 "*) preflight_fail usage 2;; esac
    preflight_seen="$preflight_seen$1 "
    case "$1" in
        --profile|--team-id) [ "$#" -ge 2 ] || preflight_fail usage 2;;
        *) preflight_fail usage 2;;
    esac
    case "$1" in
        --profile) preflight_profile=$2;;
        --team-id) preflight_team=$2;;
    esac
    shift 2
done
[ "${#preflight_profile}" -ge 1 ] && [ "${#preflight_profile}" -le 64 ] || preflight_fail usage 2
case "$preflight_profile" in *[!A-Za-z0-9._-]*|-*) preflight_fail usage 2;; esac
if [ -n "$preflight_team" ]; then
    [ "${#preflight_team}" -eq 10 ] || preflight_fail usage 2
    case "$preflight_team" in *[!ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789]*) preflight_fail usage 2;; esac
fi

preflight_identities=$("$tool_security" find-identity -v -p codesigning 2>/dev/null) || preflight_fail identity_unavailable 3
[ "${#preflight_identities}" -le 65536 ] || preflight_fail identity_unavailable 3
preflight_count=0 preflight_name='' preflight_found=''
while IFS= read -r preflight_line; do
    case "$preflight_line" in *'"Developer ID Application: '*')"') ;; *) continue;; esac
    preflight_candidate=${preflight_line#*\"}
    preflight_candidate=${preflight_candidate%\"}
    [ "${#preflight_candidate}" -le 200 ] || preflight_fail identity_unavailable 3
    case "$preflight_candidate" in *[!A-Za-z0-9\ .,:\'\(\)_-]*) preflight_fail identity_unavailable 3;; esac
    case "$preflight_candidate" in
        'Developer ID Application: '?*' ('[A-Z0-9][A-Z0-9][A-Z0-9][A-Z0-9][A-Z0-9][A-Z0-9][A-Z0-9][A-Z0-9][A-Z0-9][A-Z0-9]')') ;;
        *) preflight_fail identity_unavailable 3;;
    esac
    preflight_candidate_team=${preflight_candidate##*\(}
    preflight_candidate_team=${preflight_candidate_team%\)}
    if [ -n "$preflight_team" ] && [ "$preflight_candidate_team" != "$preflight_team" ]; then
        continue
    fi
    preflight_count=$((preflight_count + 1))
    [ "$preflight_count" -le 64 ] || preflight_fail identity_ambiguous 4
    preflight_name=$preflight_candidate
    preflight_found=$preflight_candidate_team
done <<EOF
$preflight_identities
EOF
[ "$preflight_count" -ge 1 ] || preflight_fail identity_missing 4
[ "$preflight_count" -eq 1 ] || preflight_fail identity_ambiguous 4

"$tool_xcrun" notarytool history --keychain-profile "$preflight_profile" --output-format json \
    > /dev/null 2>&1 || preflight_fail notary_profile_unavailable 5
/usr/bin/printf '{"schema_version":1,"identity":"%s","team_id":"%s","notary":"ok"}\n' "$preflight_name" "$preflight_found"
