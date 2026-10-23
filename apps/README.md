# apps/

Eine Datei pro App: `apps/<name>.yaml`. Der Dateiname ist der App-Name.

| Feld | Pflicht | Bedeutung |
|---|---|---|
| `name` | ja | `^[a-z][a-z0-9-]{0,30}$`, gleich dem Dateinamen |
| `repo` | ja | `https://github.com/<org>/<repo>`, muss **oeffentlich** sein |
| `owner` | ja | GitHub-User oder `org/team`, das die App verantwortet |
| `runtime` | ja | `pods` (Kubernetes, `appstore.yaml`) oder `vm` (OpenStack, `terraform/` + `packer/`) |
| `description` | ja | 10-300 Zeichen |

Schema: [`../schema/app-entry.schema.json`](../schema/app-entry.schema.json). Neue App eintragen: PR mit einer neuen Datei; der Check `validate-registry` prueft Schema, Erreichbarkeit/Sichtbarkeit des Repos und die Vertragsdateien auf dem Default-Branch.

Lokal pruefen: `python scripts/check_entry.py apps/<name>.yaml`.

Hinweis: Die bestehenden Eintraege sind VM-Apps (Terraform/Packer). Sie haben noch keine `appstore.yaml`.
