#!/bin/bash
# run_vast.sh — start the GenTRX gradient server on a vast.ai GPU instance.
#
# Wraps run_gradients.sh with vast.ai-specific defaults:
#   - Binds to 0.0.0.0 so the validator can reach the public port mapping.
#   - Skips the tmux session (you usually run this over SSH from a laptop).
#   - After launch, prints the public URL and API key for the validator host.
#
# Vast.ai instance setup the wrapper expects:
#   - TCP port 8100 declared in the instance template so vast.ai forwards it
#     and exposes VAST_TCP_PORT_8100 in the environment.
#   - (Optional) persistent disk mounted at /workspace; clone the repo there
#     so checkpoints, .env, and val data survive instance restarts.
#
# Usage on the vast.ai instance:
#   git clone https://github.com/taos-im/sn-79 /workspace/sn-79 && cd /workspace/sn-79
#   ./run_vast.sh -G aggregator -V <validator-uid> -e wss://<subtensor-endpoint>
#
# All run_gradients.sh flags are passed through. -b and -x defaults can be
# overridden by passing them again (last occurrence wins in getopts).

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INTERNAL_PORT=8100

# Vast.ai populates PUBLIC_IPADDR and VAST_TCP_PORT_<N> for every declared
# port. If neither is set we are probably not on a vast.ai instance.
PUBLIC_IP="${PUBLIC_IPADDR:-}"
PUBLIC_PORT_VAR="VAST_TCP_PORT_${INTERNAL_PORT}"
PUBLIC_PORT="${!PUBLIC_PORT_VAR:-}"

if [ -z "$PUBLIC_IP" ]; then
    echo " ⚠  PUBLIC_IPADDR is not set. Falling back to ifconfig.me lookup."
    PUBLIC_IP="$(curl -s --max-time 5 ifconfig.me || true)"
fi

if [ -z "$PUBLIC_PORT" ]; then
    echo "ERROR: $PUBLIC_PORT_VAR is not set."
    echo "       Declare TCP port $INTERNAL_PORT in your vast.ai instance template"
    echo "       so the gradient server is reachable from the validator host."
    exit 1
fi

if [ ! -d /workspace ]; then
    echo " ⚠  /workspace is not mounted. Checkpoints and .env will live on the"
    echo "    ephemeral disk and will be lost when the instance is destroyed."
    echo "    On instance restart the gradient server will rebootstrap from S3."
fi

# Force vast.ai-friendly defaults; user-provided -b / -x come later in $@ and
# win because run_gradients.sh's getopts loop reassigns on each occurrence.
"$REPO_ROOT/run_gradients.sh" -b 0.0.0.0 -x 0 "$@"
RC=$?

if [ "$RC" -ne 0 ]; then
    exit "$RC"
fi

API_KEY=""
if [ -f "$REPO_ROOT/.env" ]; then
    API_KEY="$(grep -E '^GENTRX_API_KEY=' "$REPO_ROOT/.env" | head -1 | cut -d= -f2- | tr -d "'\"")"
fi

VALIDATOR_URL="http://${PUBLIC_IP}:${PUBLIC_PORT}/gentrx"

echo
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo " Validator-side connection details"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo
echo "  Gradient server URL:  $VALIDATOR_URL"
[ -n "$API_KEY" ] && echo "  API key:              $API_KEY"
echo
echo "  On the validator host:"
echo "    ./run_validator.sh -Q $VALIDATOR_URL"
[ -n "$API_KEY" ] && echo "    # First run will prompt for the API key above."
echo
echo "  Verify reachability:"
echo "    curl -sS -H 'X-API-Key: \$GENTRX_API_KEY' $VALIDATOR_URL/version"
echo
