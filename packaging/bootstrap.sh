#!/bin/sh -p
# Reviewed local-candidate entry. Public publisher/URL/signing are separate gates.
# Every child uses stock absolute tools or independently authenticated seed/runtime.
# Supported explicit invocation also uses /bin/sh -p (prefer env -i). Plain sh
# overrides this shebang and can import executable functions/options at startup.
# --seed-team TEAMID (optional) adds a Developer ID check of the staged seed after its
# sha256/bytes pins and before it runs: codesign's designated requirement, exactly
# native_runtime.designated_requirement(TEAMID). The pins stay; the Team ID only narrows them.
# The staged copy keeps com.apple.quarantine (cp copies it), so Gatekeeper still evaluates
# the notarized seed at exec; a bare Mach-O cannot be stapled, so its ticket is fetched online.
# --adopt-legacy (optional, only as the final token) is the owner's migration of a 0.51/0.52 install:
# it reaches the launcher as its last token too. Install.command and the plugin never pass it.
set -eu
umask 077
# The only tool a test may redirect (in a copy of this script); every other tool is a literal.
bootstrap_codesign=/usr/bin/codesign
bootstrap_stage=''
bootstrap_seed=''
bootstrap_seed_sha=''
bootstrap_seed_bytes=''
bootstrap_archive=''
bootstrap_index=''
bootstrap_index_sha=''
bootstrap_root=''
bootstrap_codex=''
bootstrap_skills=''
bootstrap_prepare=''
bootstrap_claude_code='user'
bootstrap_team=''
bootstrap_adopt=''
bootstrap_seen=' '

bootstrap_fail() {
    printf '{"schema_version":1,"error_code":"%s","retained_stage":"%s"}\n' "$1" "$bootstrap_stage" >&2
    exit "$2"
}
bootstrap_hash() {
    [ "${#1}" -eq 64 ] || return 1
    case "$1" in *[!0123456789abcdef]*) return 1;; esac
}
bootstrap_absolute() {
    [ "${#1}" -le 4096 ] || return 1
    case "$1" in /*) ;; *) return 1;; esac
    case "$1" in *//*|*/../*|*/./*|*/..|*/.|*/) return 1;; esac
    ! printf '%s' "$1" | /usr/bin/env -i LC_ALL=C /usr/bin/grep '[[:cntrl:]]' >/dev/null
}
bootstrap_stat() {
    /usr/bin/env -i LC_ALL=C /usr/bin/stat -f '%d:%i:%u:%Lp:%z:%l:%m:%c' "$1"
}
bootstrap_metadata() {
    /usr/bin/env -i /usr/bin/plutil -extract "$1" raw -o - "$bootstrap_stage/result.json" 2>/dev/null
}
bootstrap_no_acl() {
    # The first executable cannot inspect its own admission boundary. Use only
    # stock ls metadata for these bounded paths, rejecting any extended ACL.
    # The authenticated native verifier later accepts deny-only ACLs on the
    # consumer's ancestors and inputs through descriptor-based libSystem APIs.
    bootstrap_acl=$(/usr/bin/env -i LC_ALL=C /bin/ls -ldPe "$1" 2>/dev/null) || bootstrap_fail integrity_failure 6
    [ "${#bootstrap_acl}" -le 65536 ] || bootstrap_fail integrity_failure 6
    case "$bootstrap_acl" in *'
'*) bootstrap_fail integrity_failure 6;; esac
    bootstrap_acl_mode=${bootstrap_acl%% *}
    case "$bootstrap_acl_mode" in ''|*+*) bootstrap_fail integrity_failure 6;; esac
}
bootstrap_anchor() {
    [ -d "$1" ] && [ ! -L "$1" ] || bootstrap_fail integrity_failure 6
    [ "$(/usr/bin/env -i /usr/bin/stat -f '%u' "$1")" = 0 ] || bootstrap_fail integrity_failure 6
    bootstrap_anchor_mode=$(/usr/bin/env -i /usr/bin/stat -f '%Lp' "$1")
    case "$bootstrap_anchor_mode" in ''|*[!01234567]*) bootstrap_fail integrity_failure 6;; esac
    if [ "$1" = /private/tmp ]; then
        [ "$bootstrap_anchor_mode" = 777 ] && [ "$(/usr/bin/env -i /usr/bin/stat -f '%p' "$1")" = 41777 ] || bootstrap_fail integrity_failure 6
    else
        [ "$((0$bootstrap_anchor_mode & 022))" -eq 0 ] || bootstrap_fail integrity_failure 6
    fi
    bootstrap_no_acl "$1"
}
bootstrap_input() {
    bootstrap_absolute "$1" || bootstrap_fail selection_invalid 6
    [ -f "$1" ] && [ ! -L "$1" ] || bootstrap_fail integrity_failure 6
    bootstrap_owner=$(/usr/bin/env -i /usr/bin/stat -f '%u' "$1")
    bootstrap_mode=$(/usr/bin/env -i /usr/bin/stat -f '%Lp' "$1")
    bootstrap_links=$(/usr/bin/env -i /usr/bin/stat -f '%l' "$1")
    bootstrap_size=$(/usr/bin/env -i /usr/bin/stat -f '%z' "$1")
    [ "$bootstrap_owner" = "$bootstrap_uid" ] && [ "$bootstrap_links" = 1 ] || bootstrap_fail integrity_failure 6
    case "$bootstrap_mode" in ''|*[!01234567]*) bootstrap_fail integrity_failure 6;; esac
    [ "$((0$bootstrap_mode & 022))" -eq 0 ] && [ "$bootstrap_size" -le "$2" ] || bootstrap_fail integrity_failure 6
}

while [ "$#" -gt 0 ]; do
    bootstrap_option=$1
    case "$bootstrap_seen" in *" $bootstrap_option "*) bootstrap_fail selection_invalid 6;; esac
    bootstrap_seen="$bootstrap_seen$bootstrap_option "
    case "$bootstrap_option" in
        --prepare-only) bootstrap_prepare='yes'; shift; continue;;
        --adopt-legacy)
            [ "$#" -eq 1 ] || bootstrap_fail selection_invalid 6
            bootstrap_adopt='yes'; shift; continue;;
        --json) shift; continue;;
        --seed|--seed-sha256|--seed-bytes|--archive|--index|--trusted-index-sha256|--installation-root|--codex-home|--skills-root|--seed-team)
            [ "$#" -ge 2 ] || bootstrap_fail selection_invalid 6;;
        --claude-code)
            # Claude Code registration: user scope (default), the plugin, or none. Closed values only.
            [ "$#" -ge 2 ] || bootstrap_fail selection_invalid 6
            case "$2" in user|plugin|skip) bootstrap_claude_code=$2;; *) bootstrap_fail selection_invalid 6;; esac
            shift 2; continue;;
        *) bootstrap_fail selection_invalid 6;;
    esac
    case "$bootstrap_option" in
        --seed) bootstrap_seed=$2;;
        --seed-sha256) bootstrap_seed_sha=$2;;
        --seed-bytes) bootstrap_seed_bytes=$2;;
        --archive) bootstrap_archive=$2;;
        --index) bootstrap_index=$2;;
        --trusted-index-sha256) bootstrap_index_sha=$2;;
        --installation-root) bootstrap_root=$2;;
        --codex-home) bootstrap_codex=$2;;
        --skills-root) bootstrap_skills=$2;;
        --seed-team) bootstrap_team=$2;;
    esac
    shift 2
done
bootstrap_hash "$bootstrap_seed_sha" && bootstrap_hash "$bootstrap_index_sha" || bootstrap_fail selection_invalid 6
case "$bootstrap_seed_bytes" in ''|*[!0123456789]*) bootstrap_fail selection_invalid 6;; esac
[ "${#bootstrap_seed_bytes}" -le 7 ] && [ "$bootstrap_seed_bytes" -gt 0 ] && [ "$bootstrap_seed_bytes" -le 2097152 ] || bootstrap_fail selection_invalid 6
if [ -n "$bootstrap_root" ]; then bootstrap_absolute "$bootstrap_root" || bootstrap_fail selection_invalid 6; fi
if [ -n "$bootstrap_codex" ]; then bootstrap_absolute "$bootstrap_codex" || bootstrap_fail selection_invalid 6; fi
if [ -n "$bootstrap_skills" ]; then bootstrap_absolute "$bootstrap_skills" || bootstrap_fail selection_invalid 6; fi
case "$bootstrap_seen" in *' --seed-team '*)
    [ "${#bootstrap_team}" -eq 10 ] || bootstrap_fail selection_invalid 6
    case "$bootstrap_team" in *[!0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ]*) bootstrap_fail selection_invalid 6;; esac;;
esac
bootstrap_uid=$(/usr/bin/env -i /usr/bin/id -u)
[ "$bootstrap_uid" -ne 0 ] || bootstrap_fail selection_invalid 6
[ "$(/usr/bin/env -i /usr/bin/uname -s)" = Darwin ] && [ "$(/usr/bin/env -i /usr/bin/uname -m)" = arm64 ] || bootstrap_fail unsupported_platform 3
bootstrap_macos=$(/usr/bin/env -i /usr/bin/sw_vers -productVersion)
bootstrap_major=${bootstrap_macos%%.*}
case "$bootstrap_major" in ''|*[!0123456789]*) bootstrap_fail unsupported_platform 3;; esac
[ "$bootstrap_major" -ge 26 ] || bootstrap_fail unsupported_platform 3
for bootstrap_tool in /usr/bin/openssl /usr/bin/plutil /usr/bin/mktemp /bin/cp /bin/chmod /bin/ls; do
    [ -x "$bootstrap_tool" ] || bootstrap_fail dependency_unavailable 5
done
if [ -n "$bootstrap_team" ]; then [ -x "$bootstrap_codesign" ] || bootstrap_fail dependency_unavailable 5; fi
bootstrap_input "$bootstrap_seed" 2097152
bootstrap_no_acl "$bootstrap_seed"
[ "$bootstrap_size" = "$bootstrap_seed_bytes" ] || bootstrap_fail integrity_failure 6
bootstrap_seed_before=$(bootstrap_stat "$bootstrap_seed")
bootstrap_input "$bootstrap_archive" 1073741824
bootstrap_input "$bootstrap_index" 2097152
for bootstrap_boundary in / /private /private/tmp; do bootstrap_anchor "$bootstrap_boundary"; done
bootstrap_stage=$(/usr/bin/env -i /usr/bin/mktemp -d /private/tmp/telegram-bootstrap.XXXXXXXX)
bootstrap_no_acl "$bootstrap_stage"
bootstrap_stage_before=$(bootstrap_stat "$bootstrap_stage")
trap 'bootstrap_fail interrupted 2' INT TERM HUP
/usr/bin/env -i /bin/cp "$bootstrap_seed" "$bootstrap_stage/seed"
[ "$(bootstrap_stat "$bootstrap_seed")" = "$bootstrap_seed_before" ] || bootstrap_fail integrity_failure 6
bootstrap_actual=$(/usr/bin/env -i LC_ALL=C /usr/bin/openssl dgst -sha256 "$bootstrap_stage/seed")
bootstrap_actual=${bootstrap_actual##* }
[ "$bootstrap_actual" = "$bootstrap_seed_sha" ] || bootstrap_fail integrity_failure 6
/usr/bin/env -i /bin/chmod 500 "$bootstrap_stage/seed"
bootstrap_input "$bootstrap_stage/seed" 2097152
bootstrap_no_acl "$bootstrap_stage/seed"
[ "$bootstrap_size" = "$bootstrap_seed_bytes" ] || bootstrap_fail integrity_failure 6
bootstrap_seed_copy_before=$(bootstrap_stat "$bootstrap_stage/seed")
if [ -n "$bootstrap_team" ]; then
    # Developer ID Application leaf under Apple's Developer ID CA with the pinned Team ID; an
    # unsigned, ad hoc or other-team seed never runs.
    /usr/bin/env -i LC_ALL=C "$bootstrap_codesign" --verify --strict -R "=anchor apple generic and certificate 1[field.1.2.840.113635.100.6.2.6] exists and certificate leaf[field.1.2.840.113635.100.6.1.13] exists and certificate leaf[subject.OU] = \"$bootstrap_team\"" \
        "$bootstrap_stage/seed" >/dev/null 2>&1 || bootstrap_fail seed_signature_invalid 6
    [ "$(bootstrap_stat "$bootstrap_stage/seed")" = "$bootstrap_seed_copy_before" ] || bootstrap_fail integrity_failure 6
fi
# Directory mtime changes when seed is created; bind inode/UID/mode, not mtime.
bootstrap_stage_binding=$(/usr/bin/env -i /usr/bin/stat -f '%d:%i:%u:%Lp' "$bootstrap_stage")
[ "$bootstrap_stage_binding" = "${bootstrap_stage_before%:*:*:*:*}" ] || bootstrap_fail integrity_failure 6
bootstrap_no_acl "$bootstrap_stage"
for bootstrap_boundary in / /private /private/tmp; do bootstrap_anchor "$bootstrap_boundary"; done
if /usr/bin/env -i PATH=/usr/bin:/bin:/usr/sbin:/sbin LC_ALL=C "$bootstrap_stage/seed" prepare \
    --index "$bootstrap_index" --trusted-index-sha256 "$bootstrap_index_sha" \
    --archive "$bootstrap_archive" --destination "$bootstrap_stage/release" --json > "$bootstrap_stage/result.json"; then
    :
else
    bootstrap_fail bootstrap_verification_failed 6
fi
[ "$(bootstrap_stat "$bootstrap_stage/seed")" = "$bootstrap_seed_copy_before" ] || bootstrap_fail integrity_failure 6
[ "$(/usr/bin/env -i /usr/bin/stat -f '%d:%i:%u:%Lp' "$bootstrap_stage")" = "$bootstrap_stage_binding" ] || bootstrap_fail integrity_failure 6
bootstrap_input "$bootstrap_stage/result.json" 16384
[ "$(bootstrap_metadata verified)" = true ] || bootstrap_fail integrity_failure 6
[ "$(bootstrap_metadata release_root)" = "$bootstrap_stage/release" ] || bootstrap_fail integrity_failure 6
[ "$(bootstrap_metadata index_path)" = "$bootstrap_stage/verified-index.json" ] || bootstrap_fail integrity_failure 6
[ "$(bootstrap_metadata index_sha256)" = "$bootstrap_index_sha" ] || bootstrap_fail integrity_failure 6
bootstrap_account=$(bootstrap_metadata os_home) || bootstrap_fail integrity_failure 6
bootstrap_absolute "$bootstrap_account" || bootstrap_fail integrity_failure 6
if [ -z "$bootstrap_root" ]; then bootstrap_root="$bootstrap_account/Library/Application Support/TelegramSearch"; fi
if [ -z "$bootstrap_codex" ]; then bootstrap_codex="$bootstrap_account/.codex"; fi
if [ -z "$bootstrap_skills" ]; then bootstrap_skills="$bootstrap_account/.agents/skills"; fi
if [ "$bootstrap_prepare" = yes ]; then
    /usr/bin/env -i /bin/cat "$bootstrap_stage/result.json"
    exit 0
fi
# A direct exec keeps native Terminal ancestry shell-only. Installation itself
# ends with nonsecret activation instructions; no login is attempted here.
set --
if [ "$bootstrap_adopt" = yes ]; then set -- --adopt-legacy; fi
exec /usr/bin/env -i PATH=/usr/bin:/bin:/usr/sbin:/sbin LC_ALL=C \
    "$bootstrap_stage/release/python/bin/python3" -I -S -B "$bootstrap_stage/release/launcher.py" \
    bootstrap-install --installation-root "$bootstrap_root" --codex-home "$bootstrap_codex" \
    --skills-root "$bootstrap_skills" --index "$bootstrap_stage/verified-index.json" \
    --trusted-index-sha256 "$bootstrap_index_sha" --claude-code "$bootstrap_claude_code" ${1+"$@"}
