# Доступ к домашнему телефону из GitHub Actions

Для актуального консольного пути используй [CONSOLE.md](CONSOLE.md): там Tailscale
работает обычным daemon в Termux без Android VPN-приложения. Настройки GitHub/OIDC
из разделов 2–4 ниже общие для обоих вариантов.

Телефон подключается к Tailscale и запускает обычный OpenSSH в Termux на порту **8022**.
Workflow временно подключает GitHub runner к той же сети. Проброс порта на роутере,
публичный SSH и отдельный сервис очереди команд не нужны.

Это настройка поверх текущего Android. Сборка прошивки, root и разблокировка загрузчика не требуются.
Для локального агента есть [задание с критериями готовности](LOCAL_AGENT.md).

## Что умеет workflow

Открой [Deploy to Cerro phone](https://github.com/qqrm/android_device_nubia_cerro/actions/workflows/deploy-phone.yml),
нажми **Run workflow**, выбери ветку **server-lite** и операцию:

| Операция | Результат |
| --- | --- |
| `check` | Проверяет SSH, модель устройства, Android/ABI, Python и готовность каталога. GGUF ещё не обязателен. |
| `deploy` | Собирает Android kit, проверяет artifact текущего run, обновляет код и проверяет авторизованный inference. |
| `start` | Запускает установленный сервер и проверяет его готовность. |
| `stop` | Останавливает отслеживаемый процесс сервера. SSH продолжает работать. |
| `status` | Проверяет состояние процесса. Отсутствующий или остановленный сервер даёт ошибку проверки. |

`deploy` требует заранее положить модель в `~/cerro-server/models/model.gguf`.
Модель и API-ключ при обновлении сохраняются; отсутствующий API-ключ создаётся на телефоне.
API слушает `127.0.0.1:8080`. Ключ не передаётся в runner: HTTP-проверка выполняется на телефоне.
При неудачном запуске helper пытается вернуть предыдущий код и прежнее состояние процесса.
Откат не заменяет резервную копию: отключение питания или принудительное завершение во время
замены файлов может потребовать локального восстановления.

Запуски PR и обычные сборки к телефону не подключаются. `deploy` собирает тот же commit,
из которого запущен workflow; произвольный artifact другого run не принимается.
Новый запуск не отменяет уже идущее обновление. Сборочный job не получает SSH-ключ или OIDC.

## 1. Один раз подготовить телефон

1. Установи [Termux из официального источника](https://github.com/termux/termux-app#installation)
   и [Tailscale для Android](https://tailscale.com/docs/install/android). Открой оба приложения.
   В Tailscale войди в свою сеть и запиши адрес телефона `100.x.y.z`.
2. На доверенном компьютере создай отдельный **Ed25519** ключ для этого deployment.
   Сохрани его вне checkout репозитория. Приватный ключ без парольной фразы понадобится
   GitHub Actions; публичный файл `.pub` — телефону. Пример в Bash:

   ```sh
   mkdir -p ~/.config/cerro-deploy
   chmod 700 ~/.config/cerro-deploy
   ssh-keygen -t ed25519 -N '' -C cerro-actions -f ~/.config/cerro-deploy/id_ed25519
   ```

3. Перенеси на телефон **только публичный ключ** и скрипт `bootstrap-ssh.sh` из этого
   checkout. Выполни в терминале Termux, подставив реальные пути:

   ```sh
   pkg update
   pkg install openssh python
   sh /path/to/bootstrap-ssh.sh /path/to/id_ed25519.pub --wake-lock
   ```

   Скрипт создаёт отдельную конфигурацию в `~/.ssh/cerro-sshd`, каталоги
   `~/cerro-server/models` и `~/cerro-server/config`, включает вход только по ключу,
   проверяет конфигурацию и запускает SSH. Существующие глобальные настройки OpenSSH
   и `~/.ssh/authorized_keys` сохраняются. Если порт 8022 занят другим SSH-сервером,
   сначала разберись, какой процесс его занимает; скрипт его не убивает.

4. Сохрани имя пользователя Termux и строку вида `cerro-phone ssh-ed25519 AAAA...`,
   которые напечатает скрипт. Это доверенная привязка ключа сервера: получай её из
   терминала телефона или уже авторизованного подключения. Не заменяй эту проверку
   слепым `ssh-keyscan` или `StrictHostKeyChecking=no`.

5. Для автозапуска SSH установи [Termux:Boot](https://github.com/termux/termux-boot#how-to-use)
   **из того же источника, что Termux**, и один раз открой его. Bootstrap уже создаёт
   собственный файл `~/.termux/boot/20-cerro-sshd`. Разреши фоновую работу Termux и Tailscale
   в настройках батареи, при необходимости включи Always-on VPN для Tailscale.
   Не переключай VPN, если это нарушит используемое на телефоне подключение.

`--wake-lock` включает wake lock для Termux и его boot helper. Его можно снять командой
`termux-wake-unlock`. Это не гарантия доступности: Android/прошивка могут ограничивать
процессы и сеть. Проверка с погашенным экраном и после reboot обязательна для заявления
о постоянной работе. Сам workflow телефон не перезагружает.

## 2. Разрешить CI доступ к одному SSH-порту

В политике Tailscale добавь тег `tag:cerro-ci`, alias телефона и grant.
Ниже **фрагмент для объединения с существующей политикой**, а не замена всех её правил:

```json
{
  "tagOwners": { "tag:cerro-ci": [] },
  "hosts": { "cerro-phone": "100.x.y.z" },
  "grants": [
    { "src": ["tag:cerro-ci"], "dst": ["cerro-phone"], "ip": ["tcp:8022"] }
  ]
}
```

Подставь действительный Tailscale IP телефона. Grants разрешают доступ суммарно:
если уже есть широкое правило `* -> *`, узкий grant не ограничит его.
Проверь и сузь правила, которые дают CI лишний доступ, сохранив нужный доступ остальных устройств.
Серверную функцию **Tailscale SSH** здесь не включаем: Android использует OpenSSH из Termux.

В Tailscale **Trust credentials → Credential → OpenID Connect** создай federated identity:

| Поле | Значение |
| --- | --- |
| Issuer | `https://token.actions.githubusercontent.com` |
| Subject | `repo:qqrm@4681325/android_device_nubia_cerro@1448650588:environment:phone` |
| Custom claim `repository_id` | `1448650588` |
| Custom claim `ref` | `refs/heads/server-lite` |
| Custom claim `workflow_ref` | `qqrm/android_device_nubia_cerro/.github/workflows/deploy-phone.yml@refs/heads/server-lite` |
| Custom claim `event_name` | `workflow_dispatch` |
| Scope | Только **Auth Keys → Write** (`auth_keys`) |
| Tag | `tag:cerro-ci` |

Subject выше — ожидаемый стандартный формат с immutable IDs для этого нового репозитория.
Если его OIDC-шаблон менялся, сначала проверь фактический subject; JWT и временные credentials
не нужно печатать в лог. Не расширяй доверие до всех репозиториев или всех веток для обхода ошибки.
Скопируй **Client ID** и **Audience**, выданные Tailscale. Постоянный Tailscale auth key
в GitHub для этой схемы не нужен.

## 3. Один раз настроить GitHub environment

В **Settings → Environments** создай environment **phone**. Для deployment branches выбери
**Selected branches and tags** и разреши только branch **server-lite**.

| Где | Имя | Что записать |
| --- | --- | --- |
| Environment variable | `PHONE_HOST` | Tailscale IP телефона или его MagicDNS-имя |
| Environment variable | `PHONE_USER` | Имя пользователя, выданное bootstrap |
| Environment variable | `TS_CLIENT_ID` | Client ID federated identity |
| Environment variable | `TS_AUDIENCE` | Audience той же identity |
| Environment secret | `PHONE_SSH_KEY` | Полный приватный Ed25519 ключ deployment |
| Environment secret | `PHONE_KNOWN_HOSTS` | Строка `cerro-phone ssh-ed25519 ...` от bootstrap |

С локальной авторизацией `gh` секреты можно загрузить прямо из файлов:

```sh
gh secret set PHONE_SSH_KEY --env phone --repo qqrm/android_device_nubia_cerro < ~/.config/cerro-deploy/id_ed25519
gh secret set PHONE_KNOWN_HOSTS --env phone --repo qqrm/android_device_nubia_cerro < ~/.config/cerro-deploy/known_hosts
```

Ключи, device IP и файлы с credentials в Git не добавлять.
Запусти `check`: успешный run подтверждает маршрут GitHub → телефон и SSH-аутентификацию.
Наличие YAML в репозитории само по себе этого не подтверждает.

## 4. Первый deployment

После успешного `check` скопируй выбранную локальную GGUF-модель один раз в
`~/cerro-server/models/model.gguf`. Например, с компьютера, который также подключён к Tailscale:

```sh
scp -P 8022 -i ~/.config/cerro-deploy/id_ed25519 \
  -o HostKeyAlias=cerro-phone \
  -o UserKnownHostsFile=~/.config/cerro-deploy/known_hosts \
  -o StrictHostKeyChecking=yes \
  /path/to/model.gguf USER@PHONE_HOST:cerro-server/models/model.gguf
```

Замени `USER` и `PHONE_HOST`; не перезаписывай модель работающего сервера.
При переносе с ADB-варианта сначала останови его через `python cerro.py stop`, чтобы
освободить порт 8080. Если сервер ранее запускался вручную в Termux, останови именно тот
процесс (например, `Ctrl+C` в его терминале). Старый `~/.termux/boot/10-cerro` из ручной
инструкции тоже может запускать второй SSH/сервер: после проверки его содержимого
сохрани копию вне boot-каталога и отключи этот прежний способ запуска.
Далее запусти workflow с операцией `deploy`.
Он проверит архив, архитектуру и checksums, обновит код, проверит `/health`, отказ запроса
без API-ключа и непустой ответ авторизованного inference-запроса.

SSH можно использовать и напрямую через `ssh -T` с теми же `-i`, `HostKeyAlias`, `UserKnownHostsFile`
и `StrictHostKeyChecking=yes`. Для доступа к API добавь
`-N -L 127.0.0.1:8081:127.0.0.1:8080`. SSH-ключ и API-ключ — разные credentials.
Deployment-ключ допускает команды и SFTP; выделение PTY отключено.
Для ручного API-запроса безопасно получи `~/cerro-server/config/api-key` по доверенному
SSH/SFTP и сохрани локально с ограниченными правами.

Автозапуск inference после reboot настраивается отдельно после успешного ручного deployment:
boot helper должен вызывать
`CERRO_SERVER_DIR="$HOME/cerro-server" sh "$HOME/cerro-server/device/run-server.sh" start`.
Это сохраняет проверку API-ключа, pidfile и блокировки из штатного launcher.

## Проверенные источники

- [Tailscale GitHub Action](https://tailscale.com/docs/integrations/github/github-action)
  и [OIDC](https://tailscale.com/docs/features/workload-identity-federation).
- [GitHub OIDC claims](https://docs.github.com/en/actions/reference/security/oidc).
- [Tailscale grants](https://tailscale.com/docs/reference/syntax/grants)
  и [платформы Tailscale SSH](https://tailscale.com/docs/features/tailscale-ssh).
- [Android Always-on VPN](https://developer.android.com/develop/connectivity/vpn)
  и [Doze](https://developer.android.com/training/monitoring-device-state/doze-standby).

Сборочные проверки и проверки транспортного кода выполняются в CI. SSH на NX721J,
реальный inference, работа с выключенным экраном и восстановление после reboot требуют
отдельного аппаратного прогона; результаты такого прогона не следует подменять CI-тестами.
