# app-registry

**Registry of DHBW AppStore apps and a standard CI/CD pipeline (reusable GitHub Actions workflow) for app repositories.**

Dieses Repo enthaelt
- `apps/<name>.yaml` - Registry-Eintraege aller bekannten AppStore-Apps (Name, Repo, Owner, Runtime, Beschreibung),
- `.github/workflows/app-ci.yml` - wiederverwendbare Pipeline: Vertrag pruefen, Dockerfile linten, bauen, scannen, nach ghcr.io pushen,
- `templates/app-ci-caller.yml` - die Datei, die ein App-Repo kopiert,
- `scripts/validate_appstore_yaml.py` - Validator fuer den App-Vertrag v2 (`appstore.yaml`), nur PyYAML,
- `schema/app-entry.schema.json` - JSON-Schema der Registry-Eintraege.

Die Registry ist eine Uebersicht fuer das Team. Sie ist **nicht** die Quelle der Wahrheit des AppStores: Apps und Versionen werden im AppStore selbst registriert (siehe unten).

## Registrierte Apps (Stand 2026-10-24)

| App | Runtime | Version | Hinweis |
|---|---|---|---|
| pgadmin | pods | `v2.0.0` | zwei Container (pgAdmin + PostgreSQL/Pagila), Team-Scope |
| jupyter-notebook | pods | `v2.0.0` | JupyterLab pro Person, Egress `internet` |
| web-latex | pods | `v2.0.0` | Editor pro Person, Startpfad `/login`, optionale Starter-ZIP |
| online-ide | pods | `v2.0.0` | code-server pro Person |
| template-app | pods | `v2.0.0` | Vorlage (nginx) |
| ubuntu-app | vm | `v1.0.0` | nur VM, kein Pods-Pendant |
| windows-desktop-app | vm | `v0.3.4` | nur VM |

Aeltere `v1.x`-Tags der Pods-Apps sind VM-Versionen und bleiben nur ueber ihren Tag erreichbar.

## So kommt eine neue App in den AppStore (Schritt fuer Schritt)

### 1. App-Repo anlegen
- Repo in der Org `DHBW-AppStore-T3` (oder GitHub-Host aus `APP_GIT_ALLOWED_HOSTS`, aktuell `github.com`). **Das Repo muss oeffentlich sein**, sonst lehnt das Backend es ab.
- Pods-App: `Dockerfile` und `appstore.yaml` im Repo-Root. VM-App: `terraform/` und `packer/` (siehe [template-app](https://github.com/DHBW-AppStore-T3/template-app)).

### 2. `appstore.yaml` schreiben (nur Pods)
Der Vertrag ist `apiVersion: appstore/v2`. Wichtigste Regeln (die Quelle ist `app/k8s/spec.py` im Backend):
- Unbekannte Felder werden abgelehnt; Security-Kontext, NetworkPolicy, RBAC kann die App nicht setzen.
- `image` muss mit **Digest** gepinnt sein (`ghcr.io/dhbw-appstore-t3/<app>@sha256:<64 hex>`), Tags sind verboten, nur Registry-Praefix `ghcr.io/dhbw-appstore-t3/`.
- Genau ein Container hat `expose` (Port 1024-65535). Max. 3 Container, je max. 2 CPU / 4096 Mi, Storage max. 20 Gi.
- `egress: none` (Default) oder `internet`.

Beispiel: [`tests/fixtures/valid.appstore.yaml`](tests/fixtures/valid.appstore.yaml). Lokal pruefen:

```bash
pip install pyyaml
python scripts/validate_appstore_yaml.py appstore.yaml            # streng: Digest-Pflicht
python scripts/validate_appstore_yaml.py appstore.yaml --allow-tags
```

### 3. CI einbinden
Kopiere [`templates/app-ci-caller.yml`](templates/app-ci-caller.yml) nach `.github/workflows/ci.yml` im App-Repo:

```yaml
name: ci
on: [push, pull_request]
permissions: { contents: read, packages: write }
jobs:
  app:
    uses: DHBW-AppStore-T3/app-registry/.github/workflows/app-ci.yml@main
```

Die Pipeline macht:
1. **validate** - `appstore.yaml` gegen den Vertrag.
2. **lint** - hadolint auf dem Dockerfile.
3. **build** - buildx-Build, **Trivy** (Fehler bei behebbaren HIGH/CRITICAL-Funden), dann Push nach `ghcr.io/dhbw-appstore-t3/<app>:<commit-sha>` (auf dem Default-Branch zusaetzlich `:latest`). Bei Pull Requests wird gebaut und gescannt, aber nicht gepusht.
4. **collect** - gibt `digest`, `digests` (JSON) und `image` (`ghcr.io/...@sha256:...`) als Workflow-Outputs aus; der Digest steht auch in der Job-Summary.

Optionale Inputs (`with:`): `app-name` (Default: Repo-Name), `images` (JSON-Liste fuer mehrere Container/Dockerfiles, je `suffix`, `context`, `dockerfile`), `appstore-yaml`, `strict-image-pin`, `trivy-severity`, `push`. Es werden keine Secrets benoetigt, nur das automatische `GITHUB_TOKEN`.

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

Der Check `validate-registry` prueft Schema, Dateiname, dass das Repo erreichbar und **oeffentlich** ist und dass auf dem Default-Branch `appstore.yaml` (pods) bzw. `terraform/` + `packer/` (vm) liegen.

## Wie Versionen wirklich in den AppStore kommen (recherchiert im Backend, Branch `dev`)

| Schritt | Endpoint | Auth |
|---|---|---|
| App anlegen | `POST /apps/` (Dozent/Admin) (`git_link`, `submit_all_versions`) | Keycloak-Bearer-Token eines Nutzers |
| Version einreichen | `POST /apps/{app_id}/versions/{tag}/submit` | Owner der App oder Admin |
| Freigeben / Ablehnen / Widerrufen | `POST /apps/{app_id}/versions/{tag}/approve\|reject\|revoke` | nur Admin (`require_admin`) |

- Das Backend kennt nur **Nutzer-Tokens** (Keycloak, `OIDC_ALLOWED_CLIENTS=appstore-frontend`, plus kurzlebige LTI-Session-Tokens). Es gibt **keine Service-Tokens / API-Keys** und keinen Webhook, der Versionen automatisch registriert.
- Keycloak-Nutzer-Tokens laufen nach Minuten ab und gehoeren einer Person; sie als GitHub-Secret abzulegen ist weder haltbar noch sicher.
- Deshalb gibt es hier bewusst **keinen** automatischen Registrierungs-Job und kein `APPSTORE_API_TOKEN`. Das waere erst sinnvoll, wenn das Backend einen dedizierten Service-Account / API-Key-Typ mit Berechtigung nur fuer `submit` bekommt. Die Freigabe bleibt in jedem Fall eine Admin-Entscheidung.
- Mit `submit_all_versions: true` beim Anlegen einer oeffentlichen App werden bis zu 20 vorhandene Tags automatisch eingereicht.

## Entwicklung dieses Repos

```bash
pip install pyyaml jsonschema
python scripts/validate_appstore_yaml.py tests/fixtures/valid.appstore.yaml
python scripts/check_entry.py apps/*.yaml            # braucht Internet; --offline nur Schema
```

`main` ist per PR geschuetzt; Reviews sind nicht zwingend. Der Validator spiegelt `app/k8s/spec.py` (Backend `dev`); aendert sich der Vertrag dort, muss `scripts/validate_appstore_yaml.py` nachgezogen werden.
