#!/bin/sh -p
# Notarize the signed payload, build the release DMG from a kit folder, then sign, notarize and
# staple the DMG (CLIENTS-8). Development host only: the owner's Mac. The only credential is a
# notarytool keychain profile name; no password, API key or Apple ID ever appears in argv, the
# environment or a log. --dry-run runs every step except notarization, stapling and Gatekeeper: it
# builds the payload zip with ditto, builds and checks the DMG, signs it ad hoc and verifies that
# signature. The real run signs the DMG with Developer ID and verifies it against the designated
# requirement with the pinned Team ID (native_runtime.designated_requirement) before submitting it and
# again after stapling, in addition to spctl.
#
#   notarize_release.sh --kit DIR --dmg NEW.dmg --volume-name NAME --evidence NEW.json \
#       --payload SIGNED_DIR --dry-run
#   notarize_release.sh --kit DIR --dmg NEW.dmg --volume-name NAME --evidence NEW.json \
#       --payload SIGNED_DIR --identity 'Developer ID Application: NAME (TEAMID)' --team-id TEAMID \
#       --profile PROFILE
#
# IDENTITY may also be the certificate's SHA-1. Refusals print one fixed code and never echo an argument.
set -eu
umask 077
PATH=/usr/bin:/bin:/usr/sbin:/sbin
export PATH
unset CDPATH ENV BASH_ENV DYLD_INSERT_LIBRARIES DYLD_LIBRARY_PATH DYLD_FRAMEWORK_PATH
# Tools by absolute path only. Tests rewrite exactly this block in a copy of the script.
tool_xcrun=/usr/bin/xcrun
tool_hdiutil=/usr/bin/hdiutil
tool_ditto=/usr/bin/ditto
tool_codesign=/usr/bin/codesign
tool_spctl=/usr/sbin/spctl
tool_plutil=/usr/bin/plutil
tool_openssl=/usr/bin/openssl
# End of tools.

notary_fail() {
    /usr/bin/printf '{"schema_version":1,"error_code":"%s"}\n' "$1" >&2
    exit "$2"
}
notary_absolute() {
    [ "${#1}" -le 1024 ] || return 1
    case "$1" in /*) ;; *) return 1;; esac
    case "$1" in *//*|*/../*|*/./*|*/..|*/.|*/) return 1;; esac
    case "$1" in /tmp|/tmp/*|/private/tmp|/private/tmp/*) return 1;; esac
    ! /usr/bin/printf '%s' "$1" | /usr/bin/env -i LC_ALL=C /usr/bin/grep -q '[[:cntrl:]]'
}
notary_sha256() {
    notary_digest=$(/usr/bin/env -i LC_ALL=C "$tool_openssl" dgst -sha256 "$1") || return 1
    notary_digest=${notary_digest##* }
    [ "${#notary_digest}" -eq 64 ] || return 1
    case "$notary_digest" in *[!0123456789abcdef]*) return 1;; esac
    /usr/bin/printf '%s\n' "$notary_digest"
}

notary_kit='' notary_dmg='' notary_volume='' notary_evidence='' notary_payload='' notary_identity=''
notary_profile='' notary_team='' notary_dry='' notary_seen=' '
while [ "$#" -gt 0 ]; do
    case "$notary_seen" in *" $1 "*) notary_fail usage 2;; esac
    notary_seen="$notary_seen$1 "
    case "$1" in
        --dry-run) notary_dry=yes; shift; continue;;
        --kit|--dmg|--volume-name|--evidence|--payload|--identity|--profile|--team-id)
            [ "$#" -ge 2 ] || notary_fail usage 2;;
        *) notary_fail usage 2;;
    esac
    case "$1" in
        --kit) notary_kit=$2;;
        --dmg) notary_dmg=$2;;
        --volume-name) notary_volume=$2;;
        --evidence) notary_evidence=$2;;
        --payload) notary_payload=$2;;
        --identity) notary_identity=$2;;
        --profile) notary_profile=$2;;
        --team-id) notary_team=$2;;
    esac
    shift 2
done
notary_absolute "$notary_kit" && notary_absolute "$notary_dmg" && notary_absolute "$notary_evidence" || notary_fail usage 2
[ -d "$notary_kit" ] && [ ! -L "$notary_kit" ] || notary_fail usage 2
[ ! -e "$notary_dmg" ] && [ ! -L "$notary_dmg" ] && [ ! -e "$notary_evidence" ] && [ ! -L "$notary_evidence" ] || notary_fail usage 2
notary_name=${notary_dmg##*/}
case "$notary_name" in *[!A-Za-z0-9._-]*|.*) notary_fail usage 2;; esac
case "$notary_name" in ?*.dmg) ;; *) notary_fail usage 2;; esac
notary_parent=${notary_dmg%/*}
[ -d "$notary_parent" ] && [ ! -L "$notary_parent" ] && [ -d "${notary_evidence%/*}" ] || notary_fail usage 2
[ "${#notary_volume}" -ge 1 ] && [ "${#notary_volume}" -le 27 ] || notary_fail usage 2
case "$notary_volume" in [!A-Za-z0-9]*|*[!A-Za-z0-9]|*[!A-Za-z0-9\ ._-]*) notary_fail usage 2;; esac
notary_absolute "$notary_payload" && [ -d "$notary_payload" ] && [ ! -L "$notary_payload" ] || notary_fail usage 2
if [ "$notary_dry" = yes ]; then
    [ -z "$notary_identity$notary_profile$notary_team" ] || notary_fail usage 2
else
    [ "${#notary_profile}" -ge 1 ] && [ "${#notary_profile}" -le 64 ] || notary_fail usage 2
    case "$notary_profile" in *[!A-Za-z0-9._-]*|-*) notary_fail usage 2;; esac
    [ "${#notary_team}" -eq 10 ] || notary_fail usage 2
    case "$notary_team" in *[!0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ]*) notary_fail usage 2;; esac
    [ "${#notary_identity}" -ge 1 ] && [ "${#notary_identity}" -le 200 ] || notary_fail usage 2
    case "$notary_identity" in *[!A-Za-z0-9\ .,:\'\(\)_-]*) notary_fail usage 2;; esac
    case "$notary_identity" in
        "Developer ID Application: "?*" ($notary_team)") ;;
        *) [ "${#notary_identity}" -eq 40 ] || notary_fail usage 2
           case "$notary_identity" in *[!0123456789ABCDEF]*) notary_fail usage 2;; esac;;
    esac
fi

# A directory holds exactly what its SHA256SUMS lists, plus SHA256SUMS itself.
notary_sums_match() {
    [ -f "$1/SHA256SUMS" ] && [ ! -L "$1/SHA256SUMS" ] || return 1
    notary_listed=0
    while IFS= read -r notary_line; do
        notary_listed=$((notary_listed + 1))
        [ "$notary_listed" -le 64 ] || return 1
        notary_expected=${notary_line%%  *}
        notary_file=${notary_line#*  }
        case "$notary_file" in ''|*/*|.*|SHA256SUMS|*[!A-Za-z0-9._-]*) return 1;; esac
        [ -f "$1/$notary_file" ] && [ ! -L "$1/$notary_file" ] || return 1
        [ "$(notary_sha256 "$1/$notary_file")" = "$notary_expected" ] || return 1
    done < "$1/SHA256SUMS"
    notary_present=0
    for notary_file in "$1"/* "$1"/.[!.]* "$1"/..?*; do
        [ -e "$notary_file" ] || [ -L "$notary_file" ] || continue
        notary_present=$((notary_present + 1))
    done
    [ "$notary_present" -eq "$((notary_listed + 1))" ]
}
for notary_file in Install.command bootstrap.sh bootstrap-verifier release.tar.gz release-index.json release-pins.json SHA256SUMS; do
    [ -f "$notary_kit/$notary_file" ] && [ ! -L "$notary_kit/$notary_file" ] || notary_fail kit_incomplete 3
done
notary_sums_match "$notary_kit" || notary_fail kit_mismatch 3

case "${notary_evidence##*/}" in *.json) ;; *) notary_fail usage 2;; esac
notary_work=${notary_evidence%.json}-notary  # tool logs, beside the evidence, never in the kit or DMG
[ ! -e "$notary_work" ] && [ ! -L "$notary_work" ] || notary_fail usage 2
/bin/mkdir -m 700 "$notary_work" || notary_fail usage 2
notary_mount=$notary_work/mount
notary_attached=''
trap 'if [ -n "$notary_attached" ]; then "$tool_hdiutil" detach -force "$notary_mount" >/dev/null 2>&1 || :; fi' EXIT

# The built image, mounted read-only inside the work directory, holds exactly the kit.
notary_verify_dmg() {
    /bin/mkdir -m 700 "$notary_mount" || notary_fail dmg_mismatch 4
    notary_attached=yes
    "$tool_hdiutil" attach -readonly -nobrowse -noautoopen -mountpoint "$notary_mount" "$notary_dmg" \
        > "$notary_work/attach.log" 2>&1 || notary_fail dmg_mismatch 4
    notary_sums_match "$notary_mount" || notary_fail dmg_mismatch 4
    "$tool_hdiutil" detach "$notary_mount" > "$notary_work/detach.log" 2>&1 || notary_fail dmg_mismatch 4
    notary_attached=''
    /bin/rmdir "$notary_mount" || notary_fail dmg_mismatch 4
}

# Accepted with zero issues, else a fixed refusal; sets notary_id.
notary_submit() {
    "$tool_xcrun" notarytool submit "$1" --keychain-profile "$notary_profile" --wait --output-format json \
        > "$notary_work/$2-submit.json" 2> "$notary_work/$2-submit.log" || notary_fail notarization_failed 5
    notary_id=$("$tool_plutil" -extract id raw -o - "$notary_work/$2-submit.json" 2>/dev/null) || notary_fail notarization_failed 5
    [ "${#notary_id}" -eq 36 ] || notary_fail notarization_failed 5
    case "$notary_id" in *[!0123456789abcdef-]*) notary_fail notarization_failed 5;; esac
    [ "$("$tool_plutil" -extract status raw -o - "$notary_work/$2-submit.json" 2>/dev/null)" = Accepted ] || notary_fail notarization_rejected 6
    "$tool_xcrun" notarytool log "$notary_id" --keychain-profile "$notary_profile" "$notary_work/$2-log.json" \
        > /dev/null 2> "$notary_work/$2-log.log" || notary_fail notarization_failed 5
    [ "$("$tool_plutil" -extract status raw -o - "$notary_work/$2-log.json" 2>/dev/null)" = Accepted ] || notary_fail notarization_rejected 6
    notary_issues=$("$tool_plutil" -type issues "$notary_work/$2-log.json" 2>/dev/null) || notary_fail notarization_rejected 6
    case "$notary_issues" in
        '(any)') ;;  # JSON null: no issues
        array) [ "$("$tool_plutil" -extract issues raw -o - "$notary_work/$2-log.json" 2>/dev/null)" = 0 ] || notary_fail notarization_rejected 6;;
        *) notary_fail notarization_rejected 6;;
    esac
}

# The DMG's Developer ID designated requirement with the pinned Team ID.
notary_team_verify() {
    "$tool_codesign" --verify --strict -R "=anchor apple generic and certificate 1[field.1.2.840.113635.100.6.2.6] exists and certificate leaf[field.1.2.840.113635.100.6.1.13] exists and certificate leaf[subject.OU] = \"$notary_team\"" \
        "$notary_dmg" >> "$notary_work/codesign.log" 2>&1 || notary_fail "$1" "$2"
}

# Registers every Mach-O cdhash; the notary service does not look inside release.tar.gz. Built in the
# dry run too, so only the submission differs.
"$tool_ditto" -c -k --keepParent "$notary_payload" "$notary_work/payload.zip" \
    > "$notary_work/ditto.log" 2>&1 || notary_fail payload_zip_failed 4
[ -f "$notary_work/payload.zip" ] && [ ! -L "$notary_work/payload.zip" ] && [ -s "$notary_work/payload.zip" ] \
    || notary_fail payload_zip_failed 4
notary_zip_hash=$(notary_sha256 "$notary_work/payload.zip") || notary_fail payload_zip_failed 4
notary_zip_bytes=$(/usr/bin/env -i /usr/bin/stat -f '%z' "$notary_work/payload.zip")
if [ "$notary_dry" != yes ]; then
    notary_submit "$notary_work/payload.zip" payload
    notary_payload_id=$notary_id
fi
/bin/rm -f "$notary_work/payload.zip"
"$tool_hdiutil" create -volname "$notary_volume" -srcfolder "$notary_kit" -fs HFS+ -format UDZO "$notary_dmg" \
    > "$notary_work/hdiutil.log" 2>&1 || notary_fail dmg_failed 4
[ -f "$notary_dmg" ] && [ ! -L "$notary_dmg" ] || notary_fail dmg_failed 4
notary_verify_dmg
if [ "$notary_dry" = yes ]; then
    "$tool_codesign" --force --sign - "$notary_dmg" > "$notary_work/codesign.log" 2>&1 || notary_fail dmg_sign_failed 5
    "$tool_codesign" --verify --strict "$notary_dmg" >> "$notary_work/codesign.log" 2>&1 || notary_fail dmg_sign_failed 5
    "$tool_codesign" -d --verbose=4 "$notary_dmg" > "$notary_work/signature.txt" 2>&1 || notary_fail dmg_sign_failed 5
    [ "$(/usr/bin/grep -c -x 'Signature=adhoc' "$notary_work/signature.txt")" = 1 ] || notary_fail dmg_sign_failed 5
    notary_hash=$(notary_sha256 "$notary_dmg") || notary_fail dmg_failed 4
    notary_bytes=$(/usr/bin/env -i /usr/bin/stat -f '%z' "$notary_dmg")
    /usr/bin/printf '{"schema_version":1,"mode":"dry-run","payload":{"zip_sha256":"%s","zip_bytes":%s,"notarization":"skipped"},"dmg":{"file":"%s","sha256":"%s","bytes":%s,"contents":"matches SHA256SUMS","signature":"adhoc","verified":true},"notarization":"skipped","stapling":"skipped"}\n' \
        "$notary_zip_hash" "$notary_zip_bytes" "$notary_name" "$notary_hash" "$notary_bytes" > "$notary_evidence"
    /bin/cat "$notary_evidence"
    exit 0
fi
"$tool_codesign" --force --timestamp --sign "$notary_identity" "$notary_dmg" \
    > "$notary_work/codesign.log" 2>&1 || notary_fail dmg_sign_failed 5
notary_team_verify dmg_sign_failed 5
notary_submit "$notary_dmg" dmg
notary_dmg_id=$notary_id
"$tool_xcrun" stapler staple "$notary_dmg" > "$notary_work/staple.log" 2>&1 || notary_fail staple_failed 7
"$tool_xcrun" stapler validate "$notary_dmg" > "$notary_work/validate.log" 2>&1 || notary_fail staple_failed 7
notary_team_verify gatekeeper_refused 8
"$tool_spctl" -a -vv -t open --context context:primary-signature "$notary_dmg" \
    > "$notary_work/spctl.log" 2>&1 || notary_fail gatekeeper_refused 8
/usr/bin/grep -q ': accepted$' "$notary_work/spctl.log" || notary_fail gatekeeper_refused 8
/usr/bin/grep -q '^source=Notarized Developer ID$' "$notary_work/spctl.log" || notary_fail gatekeeper_refused 8
notary_hash=$(notary_sha256 "$notary_dmg") || notary_fail dmg_failed 4
notary_bytes=$(/usr/bin/env -i /usr/bin/stat -f '%z' "$notary_dmg")
/usr/bin/printf '{"schema_version":1,"mode":"notarized","payload":{"zip_sha256":"%s","zip_bytes":%s,"submission_id":"%s","status":"Accepted","issues":0},"dmg":{"file":"%s","sha256":"%s","bytes":%s,"contents":"matches SHA256SUMS","team_id":"%s","requirement":"developer-id-team","submission_id":"%s","status":"Accepted","issues":0},"stapler_validate":"ok","spctl":"accepted: Notarized Developer ID"}\n' \
    "$notary_zip_hash" "$notary_zip_bytes" "$notary_payload_id" "$notary_name" "$notary_hash" "$notary_bytes" "$notary_team" "$notary_dmg_id" > "$notary_evidence"
/bin/cat "$notary_evidence"
