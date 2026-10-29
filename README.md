# app-registry

**Registry of DHBW AppStore apps and a standard CI/CD pipeline (reusable GitHub Actions workflow) for app repositories.**

Dieses Repo enthaelt
- `apps/<name>.yaml` - Registry-Eintraege aller bekannten AppStore-Apps (Name, Repo, Owner, Runtime, Beschreibung),
- `.github/workflows/app-ci.yml` - wiederverwendbare Pipeline: Vertrag pruefen, Dockerfile linten, bauen, scannen, nach ghcr.io pushen,
- `.github/workflows/validate-registry.yml` - Checks dieses Repos (Eintraege, Selbsttests, Workflows), woechentlich auch ohne Aenderung,
- `.github/workflows/contract-drift.yml` - vergleicht den Validator mit dem Vertragsparser des Backends,
- `templates/app-ci-caller.yml` - die Datei, die ein App-Repo kopiert,
- `scripts/validate_appstore_yaml.py` - Validator fuer den App-Vertrag v2 (`appstore.yaml`), nur PyYAML; lehnt wie das Backend Dateien ueber 256 KiB, mit YAML-Aliasen oder tiefer als 32 Ebenen ab, und verlangt fuer jede Datei-Variable (`type: file`) eine nicht leere `ext`-Liste (z. B. `ext: [zip]`), sonst nimmt das Backend jeden Dateityp ausser Programmen und nicht pruefbaren Archiven an,
- `scripts/check_entry.py`, `scripts/check_action_refs.py`, `scripts/check_platform_limits.py` - Pruefung der Registry-Eintraege, der `uses:`-Versionen in den Workflows bzw. der Obergrenzen gegen das deployte Chart,
- `schema/app-entry.schema.json` - JSON-Schema der Registry-Eintraege,
- `tests/` - Selbsttests und Fixtures.

Die Registry ist eine Uebersicht fuer das Team. Sie ist **nicht** die Quelle der Wahrheit des AppStores: Apps und Versionen werden im AppStore selbst registriert (siehe unten).

## Registrierte Apps

| App | Runtime | Hinweis |
|---|---|---|
| pgadmin | pods | zwei Container (pgAdmin + PostgreSQL/Pagila), Team-Scope |
| jupyter-notebook | pods | JupyterLab pro Person; Internet waehlbar (Standard aus: nur `pip install`) |
| web-latex | pods | Editor pro Person, optionale Starter-ZIP |
| online-ide | pods | code-server pro Person, optionale Aufgaben-ZIP |
| java-ide | pods | code-server mit JDK 21 und Maven pro Person |
| template-app | pods | Vorlage (nginx) |
| ubuntu-app | pods | Web-Terminal (ttyd) pro Person, ohne sudo; GNU-Userland; Internet waehlbar (Standard aus: Beispiele mit `curl`) |
| windows-matlab-app | kubevirt-vm | in Entwicklung (`status: development`): Windows 11 mit MATLAB als VM pro Person (Golden-Image), Zugang per noVNC; vCPU/RAM und Netzwerk im Wizard waehlbar |

Die Version einer App ist ihr neuester Git-Tag im App-Repo: den reicht man im AppStore ein, und `rescan-images.yml` prueft das Image dieses Tags. Hier stehen bewusst keine Versionsnummern, sie veralten mit jedem Release.

Aeltere `v1.x`-Tags von jupyter-notebook, web-latex, online-ide, pgadmin und ubuntu-app sind VM-Versionen und im AppStore nicht mehr nutzbar (eine Version ohne `appstore.yaml` wird abgelehnt). `windows-desktop-app` (reine OpenStack-VM-App) ist aus der Registry entfernt; Windows laeuft kuenftig ueber KubeVirt (`windows-matlab-app`, `runtime: kubevirt-vm`).

## So kommt eine neue App in den AppStore (Schritt fuer Schritt)

### 1. App-Repo anlegen
- Repo in der Org `DHBW-AppStore-T3` (oder GitHub-Host aus `APP_GIT_ALLOWED_HOSTS`, aktuell `github.com`). **Das Repo muss oeffentlich sein**, sonst lehnt das Backend es ab.
- `appstore.yaml` im Repo-Root und das Image unter `image/` (`image/Dockerfile`, so machen es alle bestehenden Pods-Apps; mehrere Images je in einem Unterordner, z.B. `image/pgadmin`).

### 2. `appstore.yaml` schreiben
Der Vertrag ist `apiVersion: appstore/v2`. Wichtigste Regeln (die Quelle ist `app/k8s/spec.py` im Backend):
- Unbekannte Felder werden abgelehnt; Security-Kontext, NetworkPolicy, RBAC kann die App nicht setzen.
- `image` muss mit **Digest** gepinnt sein, Tags sind verboten, nur Registry-Praefix `ghcr.io/dhbw-appstore-t3/` erlaubt. Konvention fuer App-Images: `ghcr.io/dhbw-appstore-t3/apps/<app>@sha256:<64 hex>` (die Plattform-Images `backend`, `worker`, `frontend` liegen eine Ebene darueber).
- Genau ein Container hat `expose` (Port 1024-65535). `expose.path` ist die **Ingress-Route** (Prefix-Match), kein Startpfad: nur dieser Pfad und alles darunter erreicht die App, normalerweise also `/`. Der Validator warnt bei allem anderen. Max. 3 Container, Storage max. 20 Gi, je Container max. 4 CPU / 8192 Mi (Obergrenzen der Plattform, `podApps.maxCpu/maxMemory` im Helm-Chart; Stand dieses Repos: siehe Hinweis unten).
- Optional `resourceBounds` je Container (`cpu` und `memory` mit `min`/`max`): der Bereich, aus dem Nutzer beim Deployen waehlen; `resources` sind der Standardwert und muessen darin liegen, `max` darf die Obergrenzen nicht uebersteigen.
- `network` (optional, Standard: nichts erlaubt): was die Pods erreichen duerfen und was die Person beim Deployen im Wizard aendern darf. Zwei Einstellungen, jede mit denselben zwei Feldern (beide Standard `false`):

  ```yaml
  network:
    internet:   { allowed: true, default: false }   # Internet ueber TCP 80/443 (nie Cluster, Nodes, private Bereiche)
    podNetwork: { allowed: true, default: false }   # die Pods dieses Deployments untereinander (TCP und UDP, jeder Port)
  ```

  | `allowed` | `default` | Bedeutung |
  |---|---|---|
  | `false` | `false` | nie verfuegbar (nichts angeboten) |
  | `false` | `true` | immer an, keine Wahl |
  | `true` | `false` | Wahl im Wizard, startet aus |
  | `true` | `true` | Wahl im Wizard, startet an |

  Zwei Booleans decken alle vier Faelle ab, ohne ungueltige Kombination. Welche Einstellung ein Wizard-Schalter bekommt und mit welchem Wert er startet, legt allein die App fest; die Plattform nimmt nur Werte fuer Einstellungen an, die `allowed: true` haben.
  - `internet: { allowed: true, default: true }` fuer Apps, die ohne Internet nicht arbeiten koennen (Paketquellen wie Maven Central, npm, Lizenzserver), `default: false` wo es nur bequem ist (`pip install`), kein `allowed` wo die App nichts nachlaedt. Entscheidend ist, was die App tatsaechlich tut, nicht was sie koennte.
  - `podNetwork` erlaubt Programmen der Studierenden, sich zu erreichen: eigene TCP-/UDP-Server und -Clients zwischen den Pods **desselben Deployments**, nirgends sonst. Jeder Pod ist unter seinem Namen (`<workload>`) auffindbar, alle zusammen unter `peers` (DNS liefert die Adressen aller Pods; wer "alle erreichen" will, sendet an jede). Einen echten Layer-2-Broadcast (`255.255.255.255`, Multicast, `ff02::1`) gibt es nicht: er erreichte die Pods aller Deployments des Nodes und wird deshalb verworfen.
  - Die aelteren Schluessel `egress: none|internet`, `podNetwork: none|namespace` und `userChoices: {internet, podNetwork}` (Apps bis v2.x) werden weiter gelesen und bedeuten dasselbe; zusammen mit `network:` sind sie ein Fehler.

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
    uses: DHBW-AppStore-T3/app-registry/.github/workflows/app-ci.yml@v1
    with:
      images: '[{"suffix":"","context":"image","dockerfile":"image/Dockerfile","smoke_test":".github/smoke-test.sh"}]'
```

Die Vorlage baut und pusht nur bei Aenderungen unter `image/` (wie die bestehenden Apps), Pull Requests bauen und pruefen nur. Zusaetzlich laeuft woechentlich ein Lauf ohne Push, der das Image neu baut (mit den aktuellen Sicherheitsupdates, siehe *Sicherheitsupdates im Image*) und mit Trivy scannt: die Schwachstellen-Datenbank aendert sich auch dann, wenn der Code gleich bleibt. Er prueft den naechsten Build, nicht das ausgerollte Image; das macht `rescan-images.yml` (siehe unten).

Die Pipeline macht:
1. **validate** - `appstore.yaml` gegen den Vertrag.
2. **lint** - hadolint auf dem Dockerfile.
3. **build** - buildx-Build (mit dem Build-Argument `SECURITY_REFRESH`, siehe *Sicherheitsupdates im Image*), optional **Smoke-Test**, **Trivy** (Fehler bei behebbaren HIGH/CRITICAL-Funden; Funde ohne verfuegbaren Fix brechen nicht ab, es gaebe nichts zu tun, stehen aber als Bericht in der Job-Summary), dann Push nach `ghcr.io/dhbw-appstore-t3/apps/<app>:<commit-sha>` (auf dem Default-Branch zusaetzlich `:latest`). Bei Pull Requests wird gebaut und gescannt, aber nicht gepusht.
4. **collect** - gibt `digest`, `digests` (JSON) und `image` (`ghcr.io/...@sha256:...`, bei mehreren Images das alphabetisch erste) als Workflow-Outputs aus; der Digest jedes Images steht auch in der Job-Summary.

**Smoke-Test:** `smoke_test` je Image ist der Pfad eines Skripts im App-Repo (Vorlage: [`templates/smoke-test-example.sh`](templates/smoke-test-example.sh)). Die Pipeline fuehrt es mit dem lokal gebauten Image aus, **bevor** gescannt oder gepusht wird, und setzt `IMAGE` und `PLATFORM_RUN_FLAGS` (der Sicherheitskontext der Plattform: UID 1000, read-only Root, keine Capabilities, kein Privilege-Escalation). Das Skript startet das Image damit, wartet auf die App und prueft sie (Login, ein echter Request). Schlaegt es fehl, wird nichts gepusht.

#### Sicherheitsupdates im Image (`SECURITY_REFRESH`)
Base-Images werden upstream oft erst Tage nach einem Fix der Distribution neu gebaut. Damit ein Image die Fixes trotzdem sofort hat, aktualisiert es seine Pakete beim Build selbst. Das wirkt nur, wenn der Build-Cache diesen Schritt nicht aus einem frueheren Lauf wiederverwendet: eine unveraenderte `RUN apk upgrade`-Zeile bleibt sonst auf dem Paketstand ihres ersten Builds (so geschehen bei template-app und pgAdmin am 2026-10-26). Die Pipeline gibt deshalb jedem Build `SECURITY_REFRESH=<Run-ID>` mit, ein neuer Wert je Lauf. Im Dockerfile:

```dockerfile
# ... schwere, selten geaenderte Schritte (Pakete installieren, pip install) ...

# Sicherheitsupdates bei jedem CI-Build: app-ci.yml setzt SECURITY_REFRESH je Lauf neu, ab hier
# kein Build-Cache aus frueheren Laeufen.
ARG SECURITY_REFRESH
RUN apt-get update \
 && apt-get upgrade -y --no-install-recommends \
 && rm -rf /var/lib/apt/lists/*
# Alpine: RUN apk upgrade --no-cache

# ... COPY der App, USER ...
```

- **Wohin:** so spaet wie moeglich, aber noch als root und vor dem Wechsel auf den App-User. Ein neuer Wert baut diesen Schritt **und alles danach** neu; was davor steht, bleibt gecacht (z.B. TeX Live, pip-Pakete).
- **Keine Pins einzelner CVEs:** Pakete mit Mindestversionen nachzuziehen (`apk add "curl>=8.23.0-r0"`) ist mit diesem Muster unnoetig, das Upgrade nimmt immer den neuesten Stand der Distribution.
- **Was Trivy danach noch meldet**, behebt kein Paket-Upgrade: Bibliotheken in Sprach-Paketen (pip, npm, z.B. das in code-server gebuendelte), ein veraltetes Base-Image (neue Version im `FROM`) oder ein Fix, den es nur in einer neueren Distribution gibt. Dann die Abhaengigkeit oder das `FROM` aktualisieren, oder eine begruendete, befristete Ausnahme in `.trivyignore` (`exp:`).
- **Lokal** (`docker build` ohne das Argument) ist das ARG leer, der Schritt wird wie jeder andere gecacht. Dockerfiles ohne dieses ARG ignorieren das Argument.

**Wie ein Fix in die laufenden Deployments kommt.** Der woechentliche Lauf der App-CI baut frisch und ist gruen, sobald die Distribution einen Fix hat; das ausgerollte Image aendert sich dadurch nicht, denn `appstore.yaml` pinnt einen Digest. Ob die freigegebenen Images betroffen sind, prueft `rescan-images.yml` in diesem Repo (montags: das Image auf dem neuesten Versions-Tag und auf dem Default-Branch jeder App). Ist er rot: in der App einen Build auf dem Default-Branch ausloesen (Push unter `image/` oder *Run workflow*), den neuen Digest in `appstore.yaml` eintragen, Tag setzen, einreichen, freigeben (Schritte 4, 6, 7).

Optionale Inputs (`with:`): `registry-ref` (nur Sonderfaelle, siehe *Pinnen und Releases*), `app-name` (Default: Repo-Name in Kleinbuchstaben), `images` (JSON-Liste fuer mehrere Container/Dockerfiles, je `suffix`, `context`, `dockerfile`), `appstore-yaml`, `strict-image-pin`, `trivy-severity`, `push`. Es werden keine Secrets benoetigt, nur das automatische `GITHUB_TOKEN`.

#### Nur den Vertragscheck nutzen
Wer seinen eigenen Build behaelt, bindet nur [`templates/spec-check-caller.yml`](templates/spec-check-caller.yml) (`validate-spec.yml`) ein: `appstore.yaml` gegen den Vertrag (streng, Digest-Pflicht) und die Pruefung, dass jedes gepinnte Image anonym von ghcr.io gezogen werden kann, ohne zu bauen.

#### Pinnen und Releases
`@main` heisst: jede Aenderung hier gilt beim naechsten Lauf sofort fuer jede App-CI, auch eine kaputte. Die Vorlagen nutzen deshalb das Release-Tag `v1` (`app-ci.yml@v1`). GitHub loest `@v1` bei jedem Lauf auf den Commit auf, auf den das Tag zeigt; Aenderungen an `main` erreichen die Apps erst mit einem Release. Der Validator kommt dabei automatisch aus demselben Stand wie die Pipeline (die Pipeline holt ihre Skripte per `job.workflow_sha` aus dem Commit, aus dem sie selbst geladen wurde; ab `v1.1.0`, davor Standard `main`). Der Input `registry-ref` ist nur noch fuer Sonderfaelle da.

**Release-Prozess** (nur Admins):
1. Aenderungen an Pipeline oder Validator per PR nach `main`; die Pflicht-Checks muessen gruen sein.
2. Bei Aenderungen an `app-ci.yml`/`validate-spec.yml` vorher in einer App ausprobieren (Probe-Branch in `template-app`, der mit `@<branch>` auf den Branch zeigt; nie mergen).
3. Tag setzen, dann das bewegliche Major-Tag darauf verschieben: `git tag -a vX.Y.Z -m "..." <commit> && git push origin vX.Y.Z && git tag -fa vX -m "..." <commit> && git push -f origin vX`; optional ein GitHub Release mit Notizen auf `vX.Y.Z`. Kompatible Aenderungen (Bugfixes, neue optionale Inputs) bekommen ein neues Minor/Patch, inkompatible ein neues Major (`v2`), das Apps bewusst per Edit von `uses:` uebernehmen.

Die Tags `v*` sind per Ruleset geschuetzt: nur Repo-Admins duerfen sie anlegen, verschieben oder loeschen (Settings > Rules > Rulesets > *protect-release-tags*). Das ist bewusst nicht automatisiert: ein Workflow mit dem normalen `GITHUB_TOKEN` kann ein Ruleset nicht umgehen (GitHub erlaubt das nur installierten GitHub Apps), und ein gespeicherter Admin-Token waere ein groesseres Risiko als der seltene manuelle Schritt. Wer es ganz unveraenderlich will, pinnt eine App auf einen Commit-SHA (`app-ci.yml@<sha>`); dann braucht jede Pipeline-Aenderung einen PR in jeder App.

**Actions in diesem Repo** sind auf Commits gepinnt (`uses: actions/checkout@<sha> # v7`), weil ein Tag im fremden Repo verschoben werden kann. Dependabot (`.github/dependabot.yml`) schlaegt woechentlich neue Staende vor und passt den Versionskommentar mit an. In *Settings > Actions > General* ist *Require actions to be pinned to a full-length commit SHA* eingeschaltet: ein Workflow mit ungepinnter Action laeuft nicht. Wiederverwendbare Workflows (`uses: owner/repo/.github/workflows/x.yml@v1`) sind davon ausgenommen. Ein neues `uses:` also mit Commit-SHA und Kommentar eintragen (`gh api repos/<owner>/<repo>/commits/<tag> --jq .sha`).

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
runtime: pods              # pods | kubevirt-vm
description: "Mindestens zehn Zeichen, maximal 300."
```

Der Check `validate-registry` prueft
- Schema, Dateiname, kein Repo doppelt eingetragen,
- das Repo ist erreichbar, **oeffentlich** und nicht archiviert, und hat ein Versions-Tag,
- `appstore.yaml` liegt auf dem Default-Branch, erfuellt den Vertrag **streng** (Digest-Pflicht), heisst wie der Eintrag, hat `runtime: kubernetes`, und jedes Image ist ohne Zugangsdaten von ghcr.io ziehbar (Paket auf *Public*),

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

`main` ist per PR geschuetzt; Reviews sind nicht zwingend. Pflicht-Checks (Branch-Schutz) sind die drei Jobs von `validate-registry`: *Registry-Eintraege pruefen*, *Selbsttests (Validator, Eintrags-Pruefung)* und *Workflows pruefen (actionlint, Action-Versionen)*. Deshalb hat dieser Workflow bei Pull Requests keinen `paths`-Filter (ein Pflicht-Check, der wegen des Filters nicht startet, bleibt "erwartet" und blockiert den Merge). `contract-drift` ist bewusst **kein** Pflicht-Check: er haengt am Backend, und eine Aenderung dort soll nicht jeden fremden PR blockieren. Der Validator spiegelt `app/k8s/spec.py` (Backend `dev`); aendert sich der Vertrag dort, muss `scripts/validate_appstore_yaml.py` nachgezogen werden. Der Workflow `contract-drift` merkt das (woechentlich und bei Aenderungen am Validator): er laesst Validator und Backend-Parser dieselben verfaelschten Dateien bewerten und schlaegt bei abweichendem Urteil fehl. `resourceBounds` kennt der Validator schon; im Backend steht es auf `feat/resource-overrides` und ist noch nicht in `dev` (der Drift-Test prueft es automatisch, sobald das Backend es hat).

**Obergrenzen:** CPU/RAM/Speicher sind Konfiguration der Plattform (Helm-Chart `podApps.maxCpu/maxMemory/maxStorage`), nicht Teil des Parsers. Der Validator nutzt 4 CPU / 8 GiB (deployment#119, noch offen); das deployte Chart hat 2 CPU / 4 GiB. Solange das so ist, akzeptiert die CI Specs, die der AppStore ablehnt (`check_platform_limits.py` meldet das im Workflow `contract-drift` als Warnung). Nach dem Merge von deployment#119 verschwindet die Warnung.

Lokaler Drift-Test: `spec.py` aus dem Backend holen (`pip install pydantic`), dann `BACKEND_SPEC_PY=<pfad>/spec.py python -m unittest discover -s tests -p test_contract_drift.py`.
