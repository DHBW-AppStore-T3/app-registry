#!/usr/bin/env bash
# Beispiel fuer einen Smoke-Test (Eintrag `smoke_test` in `images` der Pipeline); als .github/smoke-test.sh in den
# App-Repos ablegen und an die App anpassen. Die Pipeline fuehrt ihn mit dem lokal gebauten Image aus, bevor
# gescannt oder gepusht wird, und setzt:
#   IMAGE               Referenz des Images (scan-target:<sha>)
#   PLATFORM_RUN_FLAGS  der Sicherheitskontext der Plattform: --user 1000:1000 --read-only --cap-drop ALL
#                       --security-opt no-new-privileges --network host
# Das Skript startet das Image damit selbst (zusaetzlich die Pfade, die appstore.yaml unter writablePaths/storage
# schreibbar macht, als --tmpfs und die env aus appstore.yaml), wartet auf die App und prueft sie. Es schlaegt mit
# einem Exit-Code != 0 fehl; aufgeraeumt wird von der Pipeline.
set -euo pipefail

# shellcheck disable=SC2086  # PLATFORM_RUN_FLAGS soll in Woerter zerfallen
docker run -d --name smoke $PLATFORM_RUN_FLAGS --tmpfs /tmp "$IMAGE"

ok=0
for _ in $(seq 1 30); do
  # Der Cluster ist IPv6-primaer; die App muss auf [::1] antworten.
  if curl -fsS -o /dev/null "http://[::1]:8080/"; then ok=1; break; fi
  sleep 2
done
docker logs smoke
[ "$ok" = 1 ]

# Hier die eigentliche Pruefung: Login, ein Request, der etwas Echtes tut, ...
curl -fsS "http://[::1]:8080/" | grep -qi "<html"
