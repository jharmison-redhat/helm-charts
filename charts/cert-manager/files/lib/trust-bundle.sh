# Shared PEM-trust program for this chart's cluster-trust hooks: the Secret
# certificate extractor, the Proxy trustedCA reader, and the PEM block
# decomposition every bundle classification builds on. Each hook script
# sources this file from its own directory (the lib is delivered into the
# same ConfigMap as the scripts) and keeps its object-specific compute
# logic — merge/remove patches, adoption, and the reference writes — local
# to itself.
#
# PEM parsing tolerates \r?\n line endings on the markers and body lines
# (bring-your-own bundles and Secrets are frequently CRLF-encoded), but
# recognized blocks are kept VERBATIM: a foreign block's CRLF endings are
# never rewritten. Only a block's missing final newline is restored when
# content is rewritten, so blocks never fuse together. This chart's own
# blocks are always written canonically (LF).

# jq definitions shared by every bundle filter. They rely on the caller
# passing --arg key <bundle data key>. pem_guard decomposes the bundle
# content into recognized PEM blocks plus inter-block whitespace and halts
# with the object-specific diagnostic (exit 4) otherwise; $fatal_empty
# makes a non-empty, certificate-free content fatal (the set hook refuses
# to touch it, the remove hook merely finds nothing of its own).
TRUST_PEM_JQ_DEFS='
def pem_re:
  "-----BEGIN CERTIFICATE-----\\r?\\n[\\s\\S]*?-----END CERTIFICATE-----\\r?\\n?";
def pem_blocks:
  [scan(pem_re)];
def pem_term:
  if endswith("\n") then . else . + "\n" end;
def pem_leftover:
  gsub(pem_re; "");
def pem_guard($obj; $tail; $fatal_empty):
  (.data[$key] // "") as $raw
  | ([$raw | scan("-----BEGIN CERTIFICATE-----")] | length) as $begins
  | ([$raw | scan("-----END CERTIFICATE-----")] | length) as $ends
  | if $begins != $ends then
      ($obj + " content contains mismatched PEM block markers — refusing "
       + "to modify unparseable trust content; " + $tail)
      | halt_error(4)
    else . end
  | if (($raw | pem_leftover) | test("^\\s*$") | not) then
      ($obj + " content holds unrecognized non-PEM regions that a rewrite "
       + "would drop — refusing to modify unparseable trust content; "
       + $tail)
      | halt_error(4)
    else . end
  | [$raw | pem_blocks[] | pem_term] as $have
  | if $fatal_empty == "true"
      and ($raw | length) > 0 and ($have | length) == 0 then
      ($obj + " content holds no PEM certificates (whitespace or a foreign "
       + "format) — refusing to modify it; " + $tail)
      | halt_error(4)
    else . end
  | $have;
'

# Python counterpart of the jq definitions, exec'd by the hooks' python3
# heredocs (the lib exports it; the heredocs read it from the environment).
TRUST_PEM_PY='
import re
import sys

_PEM_RE = re.compile(
    r"-----BEGIN CERTIFICATE-----\r?\n.*?-----END CERTIFICATE-----\r?\n?",
    re.S)


def pem_blocks(raw):
    """The recognized PEM CERTIFICATE blocks in raw, verbatim, in order."""
    return [m.group(0) for m in _PEM_RE.finditer(raw)]


def pem_term(block):
    """Restores a missing final newline so blocks never fuse."""
    return block if block.endswith("\n") else block + "\n"


def pem_leftover(raw):
    """The text raw carries outside its recognized blocks."""
    return _PEM_RE.sub("", raw)


def pem_parse(raw):
    """Decomposes raw into newline-terminated PEM blocks.

    Raises ValueError carrying the failure reason ("markers", "regions",
    or "empty") when raw is not entirely recognized blocks plus
    inter-block whitespace."""
    if (raw.count("-----BEGIN CERTIFICATE-----")
            != raw.count("-----END CERTIFICATE-----")):
        raise ValueError("markers")
    if pem_leftover(raw).strip():
        raise ValueError("regions")
    blocks = [pem_term(m.group(0)) for m in _PEM_RE.finditer(raw)]
    if raw and not blocks:
        raise ValueError("empty")
    return blocks


def pem_fail(obj, tail, reason):
    """Writes the object-specific unparseable-content diagnostic to stderr
    and exits 4, the fail-closed compute-failure contract of the hook
    scripts."""
    detail = {
        "markers": "contains mismatched PEM block markers — refusing to "
                   "modify unparseable trust content",
        "regions": "holds unrecognized non-PEM regions that a rewrite would "
                   "drop — refusing to modify unparseable trust content",
        "empty": "holds no PEM certificates (whitespace or a foreign "
                 "format) — refusing to modify it",
    }[reason]
    sys.stderr.write(f"{obj} {detail}; {tail}\n")
    sys.exit(4)
'
export TRUST_PEM_PY

# Prints the canonical (LF, newline-terminated) first PEM certificate block
# from the Secret JSON in $1; prints nothing when tls.crt is absent or
# carries no certificate, and fails when the value is not decodable base64.
# A CRLF-encoded tls.crt is accepted and canonicalized.
secret_cert_block() {
  case "$JSON_TOOL" in
    jq)
      jq -r --arg key "$SECRET_KEY" '
        (.data[$key] // "") as $b64
        | if $b64 == "" then ""
          else ($b64 | @base64d) as $pem
          | ([$pem | scan("-----BEGIN CERTIFICATE-----\\r?\\n[\\s\\S]*?-----END CERTIFICATE-----")][0] // "")
          | gsub("\\r\\n"; "\n")
          | if endswith("\n") then . else . + "\n" end
          end' "$1"
      ;;
    python3)
      python3 - "$1" <<'PYEOF'
import base64, json, os, re, sys

with open(sys.argv[1]) as f:
    doc = json.load(f)
b64 = (doc.get("data") or {}).get(os.environ["SECRET_KEY"]) or ""
if b64:
    pem = base64.b64decode(b64).decode("utf-8", "replace")
    match = re.search(r"-----BEGIN CERTIFICATE-----\r?\n"
                      r".*?-----END CERTIFICATE-----", pem, re.S)
    if match:
        block = match.group(0).replace("\r\n", "\n")
        if not block.endswith("\n"):
            block += "\n"
        sys.stdout.write(block)
PYEOF
      ;;
  esac
}

# Prints spec.trustedCA.name from the live Proxy object JSON in $1; an
# absent, null, or empty field prints an empty string (unset).
proxy_reference() {
  case "$JSON_TOOL" in
    jq)
      jq -r '.spec.trustedCA.name // ""' "$1"
      ;;
    python3)
      python3 - "$1" <<'PYEOF'
import json, sys

with open(sys.argv[1]) as f:
    doc = json.load(f)
print((((doc.get("spec") or {}).get("trustedCA") or {}).get("name")) or "")
PYEOF
      ;;
  esac
}
