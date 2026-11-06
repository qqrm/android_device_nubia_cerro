# SSH и собственная установка в Termux

Для Termux бинарники, модель и API-ключ устанавливаются в его домашний каталог, отдельно от ADB-каталога `/data/local/tmp`.

Для доступа из GitHub Actions через домашний NAT есть [готовый deploy-workflow и bootstrap](DEPLOY.md).
Этот документ описывает ручную установку. Выбери один способ настройки SSH:
ручной `sshd` ниже и отдельный bootstrap не могут одновременно занимать порт 8022.

## 1. Включить SSH с входом по ключу

Установи [Termux из официального источника](https://github.com/termux/termux-app#installation)
и один раз открой приложение. В его терминале:

```sh
pkg update
pkg install openssh
mkdir -p ~/.ssh ~/cerro-server/models ~/cerro-server/config
chmod 700 ~/.ssh ~/cerro-server ~/cerro-server/config
```

Добавь **публичный** SSH-ключ компьютера (содержимое файла `.pub`) одной строкой в
`~/.ssh/authorized_keys`, сохранив существующие ключи. Приватный SSH-ключ остаётся на ПК.
Затем выполни в Termux:

```sh
chmod 600 ~/.ssh/authorized_keys
sshd -p 8022 -o PubkeyAuthentication=yes -o PasswordAuthentication=no -o KbdInteractiveAuthentication=no -o AuthenticationMethods=publickey
whoami
ssh-keygen -lf "$PREFIX/etc/ssh/ssh_host_ed25519_key.pub"
```

`whoami` даёт имя пользователя, например `u0_a123`. Сверь fingerprint при первом SSH-входе.
В командах с ПК замени `USER` этим именем, `PHONE_IP` — адресом телефона из настроек Wi-Fi.
Параметры требуют вход по ключу; если порт 8022 уже занят прежним `sshd`, останови его и повтори запуск.

## 2. Доступ к уже запущенному API через туннель

В отдельном терминале компьютера оставь работающим SSH-туннель:

```sh
ssh -N -p 8022 -L 127.0.0.1:8081:127.0.0.1:8080 USER@PHONE_IP
```

В каталоге распакованного набора на ПК выполни `python cerro.py probe --local-port 8081`.
Это HTTP-проверка через туннель; ADB для неё не нужен. При своём API-ключе добавь `--key-file PATH`.
API на телефоне остаётся на localhost. SSH-туннель может обращаться и к API,
запущенному через ADB, но не даёт Termux доступ к его файлам.

## 3. Перенести сервер в домашний каталог Termux

Для самостоятельного запуска из Termux сначала останови ADB-вариант: `python cerro.py stop`.
На компьютере укажи реальные пути к архиву, модели и API-ключу, созданному `deploy`:

```sh
scp -P 8022 cerro-server-android-arm64-cpu.tar.gz USER@PHONE_IP:cerro-server-android-arm64-cpu.tar.gz
scp -P 8022 /path/to/model.gguf USER@PHONE_IP:model.gguf
scp -P 8022 HOST_API_KEY USER@PHONE_IP:cerro-api-key
```

`HOST_API_KEY` — путь из [основной инструкции](../README.md), **не приватный SSH-ключ**.
В Termux:

```sh
cd ~
tar -xzf cerro-server-android-arm64-cpu.tar.gz
mv model.gguf cerro-server/models/model.gguf
mv cerro-api-key cerro-server/config/api-key
chmod 600 cerro-server/config/api-key
chmod 700 cerro-server/bin/llama-server cerro-server/bin/llama-bench
cd ~/cerro-server
sha256sum -c SHA256SUMS
grep -qxE '[0-9a-f]{64}' config/api-key && [ "$(grep -c '^' config/api-key)" = 1 ] || exit 1
./bin/llama-server --model models/model.gguf --host 127.0.0.1 --port 8080 --api-key-file config/api-key --n-gpu-layers 0 --ctx-size 2048 --threads 4 --parallel 1 --offline --no-ui
```

Последняя команда работает на переднем плане; остановка — `Ctrl+C`.
Оставь её работающей, открой туннель с ПК и повтори `probe --local-port 8081`.
Это отдельная установка: команды `cerro.py start/stop/status` управляют только ADB-каталогом.

## 4. Автозапуск — после ручной проверки

[Termux:Boot](https://github.com/termux/termux-boot#how-to-use) установи из **того же источника**,
что и Termux: их подписи должны совпадать. Один раз открой иконку Termux:Boot.
Создай каталог (`mkdir -p ~/.termux/boot`), файл `~/.termux/boot/10-cerro` с содержимым ниже и задай ему `chmod 700`:

```sh
#!/data/data/com.termux/files/usr/bin/sh
termux-wake-lock
sshd -p 8022 -o PubkeyAuthentication=yes -o PasswordAuthentication=no -o KbdInteractiveAuthentication=no -o AuthenticationMethods=publickey
cd "$HOME/cerro-server" || exit 1
grep -qxE '[0-9a-f]{64}' config/api-key && [ "$(grep -c '^' config/api-key)" = 1 ] || exit 1
nohup ./bin/llama-server --model models/model.gguf --host 127.0.0.1 --port 8080 --api-key-file config/api-key --n-gpu-layers 0 --ctx-size 2048 --threads 4 --parallel 1 --offline --no-ui > server.log 2>&1 < /dev/null &
```

Wake-lock удерживает CPU от сна и увеличивает расход энергии; снять его можно командой `termux-wake-unlock`.
Настрой разрешённую фоновую работу Termux в параметрах батареи телефона и проверь результат
с выключенным экраном и после перезагрузки, отдельно до и после первой разблокировки.
Android и прошивка могут завершать фоновые процессы: wake-lock не гарантирует постоянную доступность.
Эта инструкция ещё не проверена на NX721J; SSH, автозапуск и inference требуют аппаратного прогона.
