# Управление телефоном по SSH и из GitHub Actions

Обычный OpenSSH в Termux слушает порт **8022**. Локальный агент управляет телефоном
по домашней сети или [USB forwarding](CONSOLE.md). Для кнопки в GitHub Actions
телефонный job выполняет **домашний Linux runner**; сборка остаётся на GitHub-hosted
runner. Внешний сервер, VPN и проброс публичного SSH-порта для этой схемы не нужны.

Это настройка поверх текущего Android, без прошивки, root или изменений загрузчика.
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
| `status` | Проверяет процесс; отсутствующий или остановленный сервер даёт ненулевой exit code. |

`deploy` требует модель в `~/cerro-server/models/model.gguf`. Модель и API-ключ
сохраняются; отсутствующий API-ключ создаётся на телефоне. API слушает
`127.0.0.1:8080`, HTTP-проверка выполняется на телефоне, ключ в runner не передаётся.
При ошибке helper пытается восстановить предыдущий код и состояние процесса;
отключение питания во время замены файлов может потребовать локального восстановления.

Обычные сборки и PR к телефону не подключаются. `deploy` использует artifact
того же run и commit; новый запуск не отменяет уже идущее обновление.
Сборочный job не получает SSH-ключи.

## 1. Один раз подготовить телефон

Если bootstrap уже прошёл, используй существующие ключи и сразу проверь SSH
по [CONSOLE.md](CONSOLE.md). Повторно создавать ключи не нужно.

1. Установи [Termux из официального источника](https://github.com/termux/termux-app#installation)
   и открой его.
2. На доверенном компьютере создай отдельный Ed25519 ключ без парольной фразы
   для автоматического deployment. Храни его вне checkout с ограниченными правами.
   Пример для Bash:

   ```sh
   mkdir -p ~/.config/cerro-deploy
   chmod 700 ~/.config/cerro-deploy
   ssh-keygen -t ed25519 -N '' -C cerro-actions -f ~/.config/cerro-deploy/id_ed25519
   ```

3. Передай на телефон **только `.pub`** и `bootstrap-ssh.sh` из этого checkout.
   В терминале Termux, подставив реальные пути, выполни:

   ```sh
   pkg update
   pkg install openssh python
   sh /path/to/bootstrap-ssh.sh /path/to/id_ed25519.pub --wake-lock
   ```

   Bootstrap создаёт отдельную конфигурацию `~/.ssh/cerro-sshd`, каталоги сервера
   и SSH с входом только по ключу. Глобальная конфигурация и `~/.ssh/authorized_keys`
   сохраняются. Если порт 8022 занят, сначала выясни, какой сервис его использует.

4. Сохрани username и строку `cerro-phone ssh-ed25519 ...` из доверенного вывода
   bootstrap; строку запиши в локальный `known_hosts`. Слепой `ssh-keyscan`
   и `StrictHostKeyChecking=no` не заменяют этот host pin.
5. Проверь SSH через USB или LAN. Для автозапуска SSH установи
   [Termux:Boot](https://github.com/termux/termux-boot#how-to-use) из того же источника,
   что Termux, и открой его один раз. Bootstrap создаёт `~/.termux/boot/20-cerro-sshd`.
   Разреши фоновую работу Termux в настройках батареи.

`--wake-lock` действует и при запуске boot helper. Для отключения удали
`~/.ssh/cerro-sshd/keep-awake` и выполни `termux-wake-unlock` в Termux.
Доступность с выключенным экраном и после согласованного reboot проверяется отдельно;
сам workflow телефон не перезагружает.

## 2. Подготовить домашний runner для кнопки в GitHub

Для локального `phone.py` этот шаг не нужен. GitHub-hosted runner не видит домашний
LAN или USB компьютера, поэтому телефонный job требует метки
`[self-hosted, linux, cerro-phone]`.

На доступном телефону Linux-компьютере нужны Bash, Git, Python 3.10+, OpenSSH,
`tar`, `sha256sum` и актуальный GitHub Actions runner. Зарегистрируй его по
[инструкции GitHub](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/add-runners)
в **Settings → Actions → Runners**, добавив собственную метку `cerro-phone`.
Регистрационный токен передавай только процедуре регистрации.

До запуска workflow выполни `phone.py check` под пользователем runner и из его
среды. На Windows локальный `phone.py` работает непосредственно; для Linux runner
в WSL отдельно проверь LAN/USB-маршрут. `127.0.0.1:18022` годится, только если
forward доступен в той же среде, где запускается job. Не считай Windows localhost
и WSL localhost взаимозаменяемыми.

Репозиторий публичный: выдели runner изолированную среду без других личных ключей.
В **Settings → Actions → General** потребуй approval для всех outside collaborators
и проверяй изменения workflow перед разрешением внешнего run. Домашний runner
предназначен для ручного workflow из доверенной `server-lite`; PR jobs оставь hosted.
Метка и environment `phone` не защищают runner от другого workflow, который может
выбрать его без этого environment. GitHub описывает риск self-hosted runners
в публичных репозиториях в инструкции выше.

Убедись, что runner online и слушает задания. Без него телефонный job останется
в очереди; успешная hosted-сборка не означает, что телефон подключён.

## 3. Настроить GitHub environment

В **Settings → Environments** создай **phone**. Для deployment branches выбери
**Selected branches and tags** и разреши только branch **server-lite**.

| Где | Имя | Значение |
| --- | --- | --- |
| Environment variable | `PHONE_HOST` | Локальный IP/DNS телефона; `127.0.0.1` при USB forwarding в среде runner |
| Environment variable | `PHONE_USER` | Username от bootstrap |
| Environment variable | `PHONE_PORT` | Необязательно: по умолчанию `8022`, для приведённого USB forward — `18022` |
| Environment secret | `PHONE_SSH_KEY` | Приватный Ed25519 deployment-ключ |
| Environment secret | `PHONE_KNOWN_HOSTS` | Строка `cerro-phone ssh-ed25519 ...` от bootstrap |

С локально авторизованным `gh` в Bash загрузи секреты напрямую из файлов:

```sh
gh secret set PHONE_SSH_KEY --env phone --repo qqrm/android_device_nubia_cerro < ~/.config/cerro-deploy/id_ed25519
gh secret set PHONE_KNOWN_HOSTS --env phone --repo qqrm/android_device_nubia_cerro < ~/.config/cerro-deploy/known_hosts
gh workflow run deploy-phone.yml --ref server-lite --repo qqrm/android_device_nubia_cerro -f operation=check
```

Ключи, device IP и файлы с credentials в Git не добавляй. Дождись результата именно
созданного run: успешный `check` подтверждает SSH из runner к телефону.

## 4. Первый deployment

После `check` скопируй уже выбранную локальную GGUF-модель в
`~/cerro-server/models/model.gguf`. Пример для LAN из Bash:

```sh
scp -P 8022 -i ~/.config/cerro-deploy/id_ed25519 \
  -o HostKeyAlias=cerro-phone \
  -o UserKnownHostsFile=~/.config/cerro-deploy/known_hosts \
  -o StrictHostKeyChecking=yes \
  /path/to/model.gguf USER@PHONE_HOST:cerro-server/models/model.gguf
```

Подставь `USER` и `PHONE_HOST`; для USB замени host на `127.0.0.1`, порт на `18022`.
Не перезаписывай модель работающего сервера. При переносе с ADB сначала останови
прежний сервер через `python cerro.py stop`. Ручной процесс Termux останови в его
терминале. Если старый `~/.termux/boot/10-cerro` запускает второй сервер, проверь
содержимое, сохрани копию вне boot-каталога и отключи именно этот прежний запуск.

Запусти workflow `deploy`. Он проверит архив и checksums, обновит код, проверит
`/health`, отказ без API-ключа и непустой ответ авторизованного inference-запроса.
Без GitHub runner можно скачать и распаковать kit по [инструкции сборки](../README.md),
затем выполнить локально `phone.py` с прежними SSH-параметрами и
`deploy --bundle PATH_TO_CERRO_SERVER`.

Для прямого SSH используй `ssh -T` с теми же портом, ключом, `HostKeyAlias`,
`UserKnownHostsFile` и `StrictHostKeyChecking=yes`. Для API добавь
`-N -L 127.0.0.1:8081:127.0.0.1:8080`. Deployment-ключ допускает команды и SFTP;
PTY отключён. API-ключ отличается от SSH-ключа: для ручного API-запроса получи
`~/cerro-server/config/api-key` по доверенному SSH/SFTP и храни локально приватно.

Автозапуск inference добавляй после успешного ручного deployment. Отдельный boot
helper должен вызывать
`CERRO_SERVER_DIR="$HOME/cerro-server" sh "$HOME/cerro-server/device/run-server.sh" start`.
Так сохраняются API authentication, pidfile и блокировки штатного launcher.

CI проверяет сборку и транспортный код. Реальный SSH, inference, выключенный экран
и восстановление после reboot требуют отдельного прогона на NX721J.
