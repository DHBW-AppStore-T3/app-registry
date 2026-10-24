# apps/

Eine Datei pro App: `apps/<name>.yaml`. Der Dateiname ist der App-Name.

| Feld | Pflicht | Bedeutung |
|---|---|---|
| `name` | ja | `^[a-z][a-z0-9-]{0,30}$`, gleich dem Dateinamen |
| `repo` | ja | `https://github.com/<org>/<repo>`, muss **oeffentlich** sein |
| `owner` | ja | GitHub-User oder `org/team`, das die App verantwortet |
| `runtime` | ja | `pods` (Kubernetes, `appstore.yaml`) oder `vm` (OpenStack, `terraform/` + `packer/`) |
| `description` | ja | 10-300 Zeichen |

Schema: [`../schema/app-entry.schema.json`](../schema/app-entry.schema.json). Neue App eintragen: PR mit einer neuen Datei; der Check `validate-registry` prueft Schema, Erreichbarkeit/Sichtbarkeit des Repos, die Vertragsdateien auf dem Default-Branch (bei `pods` auch den Inhalt von `appstore.yaml` und ob die Images oeffentlich ziehbar sind) und dass kein Repo doppelt eingetragen ist.

Lokal pruefen: `python scripts/check_entry.py apps/<name>.yaml`.

Hinweis: `pgadmin`, `jupyter-notebook`, `web-latex`, `online-ide` und `template-app` laufen als Pods (`appstore.yaml`, Tag `v2.0.0`); `ubuntu-app` und `windows-desktop-app` sind weiterhin VM-Apps.
