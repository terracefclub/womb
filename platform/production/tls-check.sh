# Body of terrace-tls-check (platform/production/tls.nix sets the configuration
# below; tests/platform/test_tls_check.py runs this file directly).
#
# Connects to this host's own HTTPS listener for every public name and verifies
# what is actually served: chain to the trust store, the name, and at least
# TLS_CHECK_MIN_DAYS days left. A failed verification prints OpenSSL's own
# reason (for example "certificate is not yet valid", which means a clock
# problem, not an untrusted chain). Prints no key material.
#
# TLS_CHECK_NAMES     space-separated public names
# TLS_CHECK_TRUST     CA file the chain must verify against
# TLS_CHECK_MIN_DAYS  days a served certificate must have left
# TLS_CHECK_CONNECT   host:port of the listener (127.0.0.1:443 on a host)
# TLS_CHECK_TIMEOUT   seconds allowed for each handshake (30 by default)

min_days=${TLS_CHECK_MIN_DAYS:?}
trust=${TLS_CHECK_TRUST:?}
connect=${TLS_CHECK_CONNECT:?}
bound=${TLS_CHECK_TIMEOUT:-30}
read -r -a names <<<"${TLS_CHECK_NAMES:?}"
# s_client's diagnostics are kept apart from what it prints of the session, so a
# diagnostic line can never become part of the served certificate below.
errors=$(mktemp)
trap 'rm -f "$errors"' EXIT

# Why a handshake with -verify_return_error failed, from s_client's diagnostics:
# the first certificate verification error (the one that stopped the handshake),
# else a connection failure, else the first OpenSSL error line. OpenSSL starts
# its own verification line with "verify error:num="; a certificate subject
# that contains the phrase appears after "depth=N " and is not taken for it.
reason() {
  local output=$1 status=$2 line
  line=$(grep -m 1 -E '^verify error:num=[0-9]+:' <<<"$output" || true)
  if [[ -n $line ]]; then
    line=${line#verify error:num=}
    echo "verify error ${line%%:*}: ${line#*:}"
  elif [[ $status == 124 ]]; then
    echo "no answer from $connect within $bound s"
  elif line=$(grep -m 1 -E 'connect:errno=|Connection refused|Connection timed out|No route to host|getaddrinfo' <<<"$output"); then
    echo "cannot connect to $connect ($line)"
  elif line=$(sed -nE '/error:[0-9A-Fa-f]+:/{s/.*error:[0-9A-Fa-f]+:([^:]*):([^:]*):([^:]*).*/\3 (\1)/p;q;}' <<<"$output") && [[ -n $line ]]; then
    echo "handshake failed: $line"
  else
    echo "handshake failed, no reason reported"
  fi
}

failed=0
for name in "${names[@]}"; do
  # The chain is verified here, the name separately below: each cause has its own message.
  status=0
  served=$(timeout "$bound" openssl s_client -connect "$connect" -servername "$name" \
    -verify_return_error -CAfile "$trust" </dev/null 2>"$errors") || status=$?
  if [[ $status != 0 ]]; then
    echo "$name: FAIL: no verified TLS session: $(reason "$(cat "$errors")" "$status")"
    failed=1
    continue
  fi
  leaf=$(sed -n '/-----BEGIN CERTIFICATE-----/,/-----END CERTIFICATE-----/p' <<<"$served")
  match=$(openssl x509 -noout -checkhost "$name" <<<"$leaf" 2>/dev/null || true)
  if [[ -z $leaf || $match != *"does match"* ]]; then
    echo "$name: FAIL: the served certificate does not name $name"
    failed=1
    continue
  fi
  end=$(openssl x509 -noout -enddate <<<"$leaf" | cut -d= -f2)
  if ! openssl x509 -noout -checkend $((min_days * 86400)) <<<"$leaf" >/dev/null; then
    echo "$name: FAIL: expires $end, fewer than $min_days days left"
    failed=1
    continue
  fi
  issuer=$(openssl x509 -noout -issuer -nameopt RFC2253 <<<"$leaf" | sed 's/^issuer=//')
  echo "$name: OK: expires $end; issuer $issuer"
done
exit "$failed"
