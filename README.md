# app-registry

**Registry of DHBW AppStore apps and a standard CI/CD pipeline (reusable GitHub Actions workflow) for app repositories.**

Dieses Repo enthaelt
- `apps/<name>.yaml` - Registry-Eintraege aller bekannten AppStore-Apps (Name, Repo, Owner, Runtime, Beschreibung),
- `.github/workflows/app-ci.yml` - wiederverwendbare Pipeline: Vertrag pruefen, Dockerfile linten, bauen, scannen, nach ghcr.io pushen,
- `.github/workflows/validate-registry.yml` - Checks dieses Repos (Eintraege, Selbsttests, Workflows), woechentlich auch ohne Aenderung,
- `.github/workflows/contract-drift.yml` - vergleicht den Validator mit dem Vertragsparser des Backends,
- `templates/app-ci-caller.yml` - die Datei, die ein App-Repo kopiert,
- `scripts/validate_appstore_yaml.py` - Validator fuer den App-Vertrag v2 (`appstore.yaml`), nur PyYAML,
- `scripts/check_entry.py`, `scripts/check_action_refs.py`, `scripts/check_platform_limits.py` - Pruefung der Registry-Eintraege, der `uses:`-Versionen in den Workflows bzw. der Obergrenzen gegen das deployte Chart,
- `schema/app-entry.schema.json` - JSON-Schema der Registry-Eintraege,
- `tests/` - Selbsttests und Fixtures.

Die Registry ist eine Uebersicht fuer das Team. Sie ist **nicht** die Quelle der Wahrheit des AppStores: Apps und Versionen werden im AppStore selbst registriert (siehe unten).

## Registrierte Apps (Stand 2026-10-24)

| App | Runtime | Version | Hinweis |
|---|---|---|---|
| pgadmin | pods | `v2.0.0` | zwei Container (pgAdmin + PostgreSQL/Pagila), Team-Scope |
| jupyter-notebook | pods | `v2.0.0` | JupyterLab pro Person, Egress `internet` |
| web-latex | pods | `v2.0.0` | Editor pro Person, Startpfad `/login`, optionale Starter-ZIP |
| online-ide | pods | `v2.0.0` | code-server pro Person |
| windows-matlab-app | pods | - | in Entwicklung (`status: development`): Wine-Desktop mit MATLAB, vCPU/RAM per `resourceBounds` waehlbar; noch kein Versions-Tag, Image per Tag statt Digest |
| template-app | pods | `v2.0.0` | Vorlage (nginx); `terraform/` + `packer/` liegen noch im Repo, zaehlen aber nicht (`appstore.yaml` macht die Version zum Pods-Release) |
| ubuntu-app | vm | `v1.0.0` | nur VM, kein Pods-Pendant |
| windows-desktop-app | vm | `v0.3.4` | nur VM |

Aeltere `v1.x`-Tags von jupyter-notebook, web-latex, online-ide und pgadmin sind VM-Versionen und bleiben nur ueber ihren Tag erreichbar (template-app hat nur `v2.0.0`).

## So kommt eine neue App in den AppStore (Schritt fuer Schritt)

### 1. App-Repo anlegen
- Repo in der Org `DHBW-AppStore-T3` (oder GitHub-Host aus `APP_GIT_ALLOWED_HOSTS`, aktuell `github.com`). **Das Repo muss oeffentlich sein**, sonst lehnt das Backend es ab.
- Pods-App: `appstore.yaml` im Repo-Root und das Image unter `image/` (`image/Dockerfile`, so machen es alle bestehenden Pods-Apps; mehrere Images je in einem Unterordner, z.B. `image/pgadmin`). VM-App: `terraform/` und `packer/` (siehe [template-app](https://github.com/DHBW-AppStore-T3/template-app)).

### 2. `appstore.yaml` schreiben (nur Pods)
Der Vertrag ist `apiVersion: appstore/v2`. Wichtigste Regeln (die Quelle ist `app/k8s/spec.py` im Backend):
- Unbekannte Felder werden abgelehnt; Security-Kontext, NetworkPolicy, RBAC kann die App nicht setzen.
- `image` muss mit **Digest** gepinnt sein, Tags sind verboten, nur Registry-Praefix `ghcr.io/dhbw-appstore-t3/` erlaubt. Konvention fuer App-Images: `ghcr.io/dhbw-appstore-t3/apps/<app>@sha256:<64 hex>` (die Plattform-Images `backend`, `worker`, `frontend` liegen eine Ebene darueber).
- Genau ein Container hat `expose` (Port 1024-65535). Max. 3 Container, Storage max. 20 Gi, je Container max. 4 CPU / 8192 Mi (Obergrenzen der Plattform, `podApps.maxCpu/maxMemory` im Helm-Chart; Stand dieses Repos: siehe Hinweis unten).
- Optional `resourceBounds` je Container (`cpu` und `memory` mit `min`/`max`): der Bereich, aus dem Nutzer beim Deployen waehlen; `resources` sind der Standardwert und muessen darin liegen, `max` darf die Obergrenzen nicht uebersteigen.
- `egress: none` (Default) oder `internet`.

Beispiel: [`tests/fixtures/valid.appstore.yaml`](tests/fixtures/valid.appstore.yaml). Lokal pruefen:

```bash
pip install pyyaml
python scripts/validate_appstore_yaml.py appstore.yaml            # streng: Digest-Pflicht
python scripts/validate_appstore_yaml.py appstore.yaml --allow-tags
```

### 3. CI einbinden
Kopiere [`templates/app-ci-caller.yml`](templates/app-ci-caller.yml) nach `.github/workflows/ci.yml` im App-Repo. Kopiert wird nur dieser kleine Aufrufer; die Pipeline selbst bleibt hier und wird bei jedem Lauf von `app-ci.yml` geholt, Aenderungen an ihr gelten also sofort fuer alle Aufrufer (siehe *Pinnen*). Im Kern:

```yaml
jobs:
  app:
    uses: DHBW-AppStore-T3/app-registry/.github/workflows/app-ci.yml@main
    with:
      images: '[{"suffix":"","context":"image","dockerfile":"image/Dockerfile","smoke_test":".github/smoke-test.sh"}]'
```

Die Vorlage baut und pusht nur bei Aenderungen unter `image/` (wie die bestehenden Apps), Pull Requests bauen und pruefen nur.

Die Pipeline macht:
1. **validate** - `appstore.yaml` gegen den Vertrag.
2. **lint** - hadolint auf dem Dockerfile.
3. **build** - buildx-Build, optional **Smoke-Test**, **Trivy** (Fehler bei behebbaren HIGH/CRITICAL-Funden), dann Push nach `ghcr.io/dhbw-appstore-t3/apps/<app>:<commit-sha>` (auf dem Default-Branch zusaetzlich `:latest`). Bei Pull Requests wird gebaut und gescannt, aber nicht gepusht.
4. **collect** - gibt `digest`, `digests` (JSON) und `image` (`ghcr.io/...@sha256:...`, bei mehreren Images das alphabetisch erste) als Workflow-Outputs aus; der Digest jedes Images steht auch in der Job-Summary.

**Smoke-Test:** `smoke_test` je Image ist der Pfad eines Skripts im App-Repo (Vorlage: [`templates/smoke-test-example.sh`](templates/smoke-test-example.sh)). Die Pipeline fuehrt es mit dem lokal gebauten Image aus, **bevor** gescannt oder gepusht wird, und setzt `IMAGE` und `PLATFORM_RUN_FLAGS` (der Sicherheitskontext der Plattform: UID 1000, read-only Root, keine Capabilities, kein Privilege-Escalation). Das Skript startet das Image damit, wartet auf die App und prueft sie (Login, ein echter Request). Schlaegt es fehl, wird nichts gepusht.

Optionale Inputs (`with:`): `registry-ref` (siehe *Pinnen*),  `app-name` (Default: Repo-Name in Kleinbuchstaben), `images` (JSON-Liste fuer mehrere Container/Dockerfiles, je `suffix`, `context`, `dockerfile`), `appstore-yaml`, `strict-image-pin`, `trivy-severity`, `push`. Es werden keine Secrets benoetigt, nur das automatische `GITHUB_TOKEN`.

#### Nur den Vertragscheck nutzen
Wer seinen eigenen Build behaelt, bindet nur [`templates/spec-check-caller.yml`](templates/spec-check-caller.yml) (`validate-spec.yml`) ein: `appstore.yaml` gegen den Vertrag (streng, Digest-Pflicht) und die Pruefung, dass jedes gepinnte Image anonym von ghcr.io gezogen werden kann, ohne zu bauen.

#### Pinnen
`@main` heisst: jede Aenderung hier gilt sofort fuer jede App-CI, auch eine kaputte. Ein Aufrufer kann auf ein Release-Tag dieses Repos pinnen (`app-ci.yml@v1`). Dann muss auch der Validator aus diesem Stand kommen, weil die Pipeline ihn selbst aus diesem Repo holt: `registry-ref: v1` in `with:` setzen. Ein Release-Tag legt jemand mit Schreibrecht nach dem Merge an (`git tag v1.0.0 && git tag -f v1 && git push origin v1.0.0 v1 -f`; `v1` wandert mit kompatiblen Updates mit, `v1.0.0` nicht).

### 4. Digest in `appstore.yaml` eintragen (Henne-Ei-Problem)
Der Vertrag verlangt `@sha256:`-Digests, der Digest existiert aber erst nach dem Build. Ablauf:
1. Erster Push: Pipeline baut und pusht. Die Validierung laeuft standardmaessig tolerant (`strict-image-pin: false`, Tags nur als Warnung).
2. Digest aus der Job-Summary (oder Output `image`) in `appstore.yaml` eintragen, committen.
3. Danach auf `strict-image-pin: true` stellen, damit nur noch Digests durchgehen.

Der Commit mit dem Digest ist der Commit, den ihr als Version einreicht (die Version ist an einen Commit gebunden).

### 5. Paket oeffentlich stellen
GitHub legt neue ghcr.io-Pakete **privat** an. Der Cluster zieht ohne Zugangsdaten: unter *Packages -> <app> -> Package settings -> Change visibility* auf *Public* stellen (einmalig pro Image; Pakete mit der Org verknuepfen, damit die Rechte stimmen).

### 6. Version taggen
Versionen sind Git-Tags (`v1.0.0`). Das Backend liest sie per `git ls-remote` und loest den Tag beim Einreichen auf einen **Commit-SHA** auf.

```bash
git tag v1.0.0 && git push origin v1.0.0
```

### 7. App und Version im AppStore einreichen
Per Frontend (als Dozent/Admin) oder direkt per API (Beispiel Staging; `$T` = Keycloak-Access-Token, `$H` = `https://appstore-staging.<zone>/api`):

```bash
# 1. App anlegen (Repo muss oeffentlich sein; Rolle teacher oder admin)
curl -X POST $H/apps/ -H "Authorization: Bearer $T" -H 'Content-Type: application/json' \
  -d '{"name":"meine-app","description":"...","git_link":"https://github.com/DHBW-AppStore-T3/meine-app","is_private":false}'
# 2. Version (Git-Tag) einreichen; das Backend loest den Tag auf einen Commit auf
curl -X POST $H/apps/$APP_ID/versions/v2.0.0/submit -H "Authorization: Bearer $T" -H 'Content-Type: application/json' -d '{}'
# 3. Admin gibt genau diesen Commit frei
curl -X POST $H/admin/apps/$APP_ID/versions/v2.0.0/approve -H "Authorization: Bearer $T" -H 'Content-Type: application/json' -d '{}'
# 4. Testweise deployen (Teams = Liste von Nutzer-IDs), danach DELETE $H/deployments/$DEPLOYMENT_ID
curl -X POST $H/deployments/ -H "Authorization: Bearer $T" -H 'Content-Type: application/json' \
  -d '{"name":"test","appId":"'$APP_ID'","releaseTag":"v2.0.0","teams":[{"name":"t1","userIds":["'$USER_ID'"]}]}'
```

Hinweise:
- `git_link` ist nach dem Anlegen unveraenderlich; `submit_all_versions: true` reicht bis zu 20 vorhandene Tags auf einmal ein (nicht sinnvoll, wenn aeltere Tags noch VM-Versionen sind).
- Ein Admin genehmigt die Version fuer genau diesen Commit. Verschiebt sich der Tag, muss neu eingereicht werden.
- Der Tag muss auf einen Commit zeigen, der eine gueltige `appstore.yaml` enthaelt (Pods). Ein Tag aus der VM-Zeit (`v1.x`) hat keine und taugt nicht als Pods-Version.
- Nach dem Deploy ein paar Sekunden warten: Pods haben keine Readiness-Probe, direkt nach "success" kann kurz 502 kommen.
- Prod: dieselben Schritte gegen `https://appstore.<zone>/api`, nur durch den Koordinator.

### 8. In die Registry eintragen
PR gegen dieses Repo mit `apps/<name>.yaml`:

```yaml
name: meine-app            # = Dateiname, ^[a-z][a-z0-9-]{0,30}$
repo: https://github.com/DHBW-AppStore-T3/meine-app
owner: DHBW-AppStore-T3    # GitHub-User oder org/team
runtime: pods              # pods | vm
description: "Mindestens zehn Zeichen, maximal 300."
```

Der Check `validate-registry` prueft
- Schema, Dateiname, kein Repo doppelt eingetragen,
- das Repo ist erreichbar, **oeffentlich** und nicht archiviert, und hat ein Versions-Tag,
- `pods`: `appstore.yaml` liegt auf dem Default-Branch, erfuellt den Vertrag **streng** (Digest-Pflicht), heisst wie der Eintrag, hat `runtime: kubernetes`, und jedes Image ist ohne Zugangsdaten von ghcr.io ziehbar (Paket auf *Public*),
- `vm`: `terraform/` + `packer/` liegen dort und `appstore.yaml` **nicht** (das Backend behandelt eine Version mit `appstore.yaml` als Pods).

Eintraege mit `status: development` (App noch nicht eingereicht) werden nur auf dem Default-Branch geprueft, duerfen Image-Tags statt Digests nutzen und brauchen weder Versions-Tag noch oeffentliches Image; alles andere gilt weiter. Sobald die App eingereicht ist, `status` entfernen.

Alle diese Pruefungen gelten fuer den Default-Branch **und** das neueste Versions-Tag (`vX.Y.Z`), denn das Backend liest `appstore.yaml` an dem Commit, auf den das eingereichte Tag zeigt; ohne Versions-Tag schlaegt der Check fehl. Er laeuft auch woechentlich ueber alle Eintraege.

## Wie Versionen wirklich in den AppStore kommen (recherchiert im Backend, Branch `dev`)

| Schritt | Endpoint | Auth |
|---|---|---|
| App anlegen | `POST /apps/` (Dozent/Admin) (`git_link`, `submit_all_versions`) | Keycloak-Bearer-Token eines Nutzers |
| Version einreichen | `POST /apps/{app_id}/versions/{tag}/submit` | Owner der App oder Admin |
| Freigeben / Ablehnen / Widerrufen | `POST /admin/apps/{app_id}/versions/{tag}/approve\|reject\|revoke` (`reject` und `revoke` brauchen `{"rejection_reason": "..."}`) | nur Admin (`require_admin`) |

- Das Backend kennt nur **Nutzer-Tokens** (Keycloak, `OIDC_ALLOWED_CLIENTS=appstore-frontend`, plus kurzlebige LTI-Session-Tokens). Es gibt **keine Service-Tokens / API-Keys** und keinen Webhook, der Versionen automatisch registriert.
- Keycloak-Nutzer-Tokens laufen nach Minuten ab und gehoeren einer Person; sie als GitHub-Secret abzulegen ist weder haltbar noch sicher.
- Deshalb gibt es hier bewusst **keinen** automatischen Registrierungs-Job und kein `APPSTORE_API_TOKEN`. Das waere erst sinnvoll, wenn das Backend einen dedizierten Service-Account / API-Key-Typ mit Berechtigung nur fuer `submit` bekommt. Die Freigabe bleibt in jedem Fall eine Admin-Entscheidung.
- Mit `submit_all_versions: true` beim Anlegen einer oeffentlichen App werden bis zu 20 vorhandene Tags automatisch eingereicht.

## Entwicklung dieses Repos

```bash
pip install pyyaml jsonschema
python -m unittest discover -s tests -v              # Selbsttests (Validator, Eintrags-Pruefung)
python scripts/validate_appstore_yaml.py tests/fixtures/valid.appstore.yaml
python scripts/check_entry.py apps/*.yaml            # braucht Internet; --offline nur Schema, Dateiname, Duplikate
python scripts/check_action_refs.py                  # loest alle uses:-Versionen der Workflows auf (Internet)
```

`main` ist per PR geschuetzt; Reviews sind nicht zwingend und die Checks sind keine Pflicht-Checks (Branch-Schutz, Einstellung in GitHub). Der Validator spiegelt `app/k8s/spec.py` (Backend `dev`); aendert sich der Vertrag dort, muss `scripts/validate_appstore_yaml.py` nachgezogen werden. Der Workflow `contract-drift` merkt das (woechentlich und bei Aenderungen am Validator): er laesst Validator und Backend-Parser dieselben verfaelschten Dateien bewerten und schlaegt bei abweichendem Urteil fehl. `resourceBounds` kennt der Validator schon; im Backend steht es auf `feat/resource-overrides` und ist noch nicht in `dev` (der Drift-Test prueft es automatisch, sobald das Backend es hat).

**Obergrenzen:** CPU/RAM/Speicher sind Konfiguration der Plattform (Helm-Chart `podApps.maxCpu/maxMemory/maxStorage`), nicht Teil des Parsers. Der Validator nutzt 4 CPU / 8 GiB (deployment#119, noch offen); das deployte Chart hat 2 CPU / 4 GiB. Solange das so ist, akzeptiert die CI Specs, die der AppStore ablehnt (`check_platform_limits.py` meldet das im Workflow `contract-drift` als Warnung). Nach dem Merge von deployment#119 verschwindet die Warnung.

Lokaler Drift-Test: `spec.py` aus dem Backend holen (`pip install pydantic`), dann `BACKEND_SPEC_PY=<pfad>/spec.py python -m unittest discover -s tests -p test_contract_drift.py`.
