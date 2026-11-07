# Консольный доступ: Termux + Tailscale без Android VPN-приложения

Этот путь использует обычные `tailscale` и `tailscaled` вместе с уже настроенным
Termux OpenSSH. Android-приложение Tailscale, его браузерный вход и PRoot не нужны.
Сам Android пока остаётся аппаратной основой; [конечная цель нативного продукта](../HEADLESS.md)
рассматривается отдельно.

Версия **1.104.4** закреплена вместе с SHA-256 официального ARM64-архива.
В этой версии upstream включает `feature/androidbin`: поддержку запуска обычного
бинарника через Termux/ADB, включая обход недоступного Android netlink и настройку DNS/CA.
Это основание для проверки на NX721J, **а не результат аппаратной проверки**.
`pkg install tailscale` здесь не используется: соответствующего пакета в официальном
дереве Termux на момент подготовки инструкции нет.

## 1. Проверить имеющийся SSH через USB

На компьютере используй уже найденный абсолютный путь к `adb`, авторизованный NX721J,
существующий deployment-ключ и host pin из доверенного bootstrap-вывода.
Выбери свободный локальный порт и настрой forwarding:

```sh
adb -s SERIAL forward tcp:18022 tcp:8022
python server/phone.py --host 127.0.0.1 --port 18022 --user TERMUX_USER --ssh-key PRIVATE_KEY_FILE --known-hosts KNOWN_HOSTS_FILE check
```

Заменить нужно serial и локальные пути; не печатай serial/секреты в публичный отчёт.
Этот тест не зависит от Wi-Fi или Tailscale и не требует GGUF-модели.

## 2. Установить и запустить CLI в Termux

Передай `tailscale-console.sh` из этого checkout в домашний каталог Termux по уже
проверенному SSH/SFTP. Если запускаешь через USB ADB, команды должны выполняться
в терминале Termux, а не от UID `shell` с чужим HOME.
Для установки нужны `curl`, `tar` и `sha256sum`; если их нет, выполни в Termux
`pkg install curl tar coreutils`. Сам helper пакеты не устанавливает.

```sh
mkdir -p "$HOME/bin"
# Сохранить скрипт как $HOME/bin/cerro-tailnet.sh.
sh "$HOME/bin/cerro-tailnet.sh" install
sh "$HOME/bin/cerro-tailnet.sh" run
```

`install` скачивает только закреплённый официальный архив и проверяет его checksum.
`run` запускает daemon на переднем плане в userspace-режиме; root и `/dev/net/tun`
не требуются. Оставь этот процесс работающим для первичной проверки.
Для команд следующего раздела открой второе SSH-подключение/сессию Termux.

## 3. Однократная авторизация из консоли

В существующем tailnet создай **одноразовый**, **не ephemeral**, не помеченный CI-тегом
ключ для телефона. Сохрани его в приватный файл Termux, например `~/cerro-tailnet-authkey`,
через защищённую передачу. Не вставляй содержимое ключа в логи или команду инструмента.

```sh
chmod 600 "$HOME/cerro-tailnet-authkey"
sh "$HOME/bin/cerro-tailnet.sh" login "$HOME/cerro-tailnet-authkey"
sh "$HOME/bin/cerro-tailnet.sh" status
```

После подтверждённого успешного входа удали одноразовый файл ключа. Постоянное состояние
daemon хранится в приватном каталоге; повторная авторизация при обычном перезапуске
не требуется. Не отправляй этот каталог в Git, artifacts или диагностические логи.

В userspace-режиме входящие Tailscale-соединения пересылаются на тот же порт localhost,
поэтому наш OpenSSH остаётся на `127.0.0.1:8022`/порту 8022. С компьютера проверь
`phone.py check` ещё раз, теперь с реальным Tailscale IP телефона и `--port 8022`.
Проверка `tailscale ping` или наличие адреса сами по себе не заменяют SSH-вход.

## 4. GitHub Actions и фоновой запуск

В GitHub `PHONE_HOST` должен содержать IP **консольного узла телефона**, а не адрес
Windows-компьютера, Android VPN-приложения или USB localhost. Username, SSH-ключ и
host pin остаются прежними. Grants и OIDC для CI настроены по [DEPLOY.md](DEPLOY.md).
CI продолжает использовать OIDC; телефонный одноразовый auth key в GitHub не сохраняется.

После успешного ручного прогона daemon можно запускать через `nohup` или отдельный
Termux:Boot helper с перенаправлением stdout/stderr в приватный локальный log и stdin
из `/dev/null`. Не запускай второй daemon с тем же state/socket.
Termux:Boot на этом этапе остаётся способом запуска Termux после загрузки Android.
Для нативного продукта его заменит init service.

Перед отключением прежнего Android-приложения Tailscale сначала проверь новый SSH-маршрут
и успешный workflow `check`. Не меняй остальные VPN-подключения без проверки их назначения.
Обязательны отдельные проверки работы с выключенным экраном и после согласованного reboot.
Удаление Android-приложения Tailscale само по себе не отменяет ограничения фоновой работы Termux.

## Источники и проверка

- [Поддержка обычных бинарников на Android в upstream 1.104.4](https://github.com/tailscale/tailscale/tree/v1.104.4/feature/androidbin).
- [Userspace networking](https://tailscale.com/docs/concepts/userspace-networking)
  и [входящие соединения на localhost](https://tailscale.com/docs/reference/faq/other-vpns).
- [Первичная авторизация ключом](https://tailscale.com/docs/features/access-control/auth-keys).

Синтаксис скрипта, checksum и формат поставляемых бинарников проверяются отдельно
от запуска на телефоне. Доступность на NX721J и её сохранение после сна/reboot
не следует считать подтверждёнными до аппаратного прогона.
