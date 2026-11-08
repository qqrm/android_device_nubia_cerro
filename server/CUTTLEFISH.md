# Cuttlefish в GitHub Actions

Workflow [`Cuttlefish native server smoke`](../.github/workflows/cuttlefish-smoke.yml)
подготавливает виртуальный Android на GitHub-hosted `ubuntu-24.04`. Он запускается
вручную и на pull request в `server-lite`, который меняет workflow, inventory,
общий builder или виртуальные тесты. Runner рядом с телефоном не нужен.

**Это проверка нативных сервисов на стандартном виртуальном Android с framework.
Она не проверяет загрузку без Zygote, `lineage_cerro` или оборудование NX721J.**

[Первый прогон](https://github.com/qqrm/android_device_nubia_cerro/actions/runs/42490329448)
успешно завершился 8 ноября 2026 года за 3 мин 50 с на commit
`425e28dc446e6a64f53140e40d332d6cc6ed1b9c`: загрузка VM, ADB, запись/чтение
данных и inventory прошли. В отчёте `failed_commands=0` и `ro.zygote=zygote64`,
то есть проверен именно стандартный Android с framework.

## Что проверяет workflow

Job `native-build` собирает отдельный тестовый комплект для Android x86_64:
`llama-server`, штатный OpenSSH (`sshd`, `sshd-session`, `sshd-auth`) со статическим
OpenSSL. Используются закреплённые commit SHA, тот же NDK/API и общие CPU-флаги
llama.cpp, что в ARM64 kit. Дополнительные x86 ISA выключены для переносимости.
Проверяются архитектура ELF, PIE, Android linker, отсутствие RPATH и зависимостей
за пределами системных библиотек Android. Это кросс-сборка приложений, не прошивки.

Для совместимости OpenSSH с публичным NDK закреплена статическая `ldns 1.9.2`
и включён штатный backend `--with-ldns`: libc fallback требует закрытой структуры
resolver Bionic. Дополнительная динамическая библиотека в госте не нужна. В копию
исходников для сборки переносится ровно одна строка
[из AOSP](https://android.googlesource.com/platform/external/openssh/+/6e3b25e80c2b4a21ec3c545bde30b8ff45a25416/openbsd-compat/explicit_bzero.c):
указатель безопасного стирания направлен на `__bionic_bzero`, поскольку `bzero`
в NDK — макрос. Защита через volatile-указатель сохраняется; SHA-256 полного файла
до и после изменения, исходная ссылка, версии и флаги записаны в provenance.
Исходные checkout остаются неизменными. Выбор поддерживаемого Clang sentinel
attribute и наличие Bionic `bzero` задаются отдельно; `bzero` предварительно
проверяется компиляцией и линковкой. Настройки аутентификации и sandbox не меняются.

Job `stock-image` получает этот комплект и запускает настоящий гостевой Android:

1. На runner доступны KVM и устройства, необходимые штатному контейнеру Cuttlefish.
   Их отсутствие завершает job ошибкой, а не успешным пропуском.
2. Неизменённый интеграционный тест Google получает готовые Android image и host
   package из одной закреплённой сборки, создаёт VM и проверяет `Running`.
   Кэширование результата теста выключено: каждый запуск должен создать VM заново.
3. ADB подключается к единственной VM из ответа orchestrator. Проверяются x86_64,
   номер сборки и стандартное `sys.boot_completed=1`.
4. В `/data/local/tmp` гостя записывается, читается и удаляется случайный маркер.
5. Неизменённый `server/device/inventory.sh` выполняется внутри гостя; ненулевой
   код возврата считается ошибкой. Его вызов `pm` требует Android framework.
6. Проверяются хеши и происхождение нативного комплекта. Бинарники и тестовая модель
   помещаются в новый `/data/local/tmp/cerro-native`; команды выполняются от `shell`
   (UID 2000). Состояние SELinux и процессов framework записывается в отчёт.
7. OpenSSH слушает loopback гостя. Тестовый сертификат от временного CA должен
   выполнить `id -u` и получить `2000`; отдельный неподписанный ключ должен быть
   отклонён именно на этапе аутентификации. Пароли выключены, `StrictModes` включён,
   ключ хоста закреплён во временном `known_hosts`.
8. `llama-server` загружает закреплённую маленькую GGUF-модель на CPU. Запрос без
   токена должен получить HTTP 401; запрос с токеном должен вернуть хотя бы один
   сгенерированный токен. Этот тест проверяет путь выполнения inference, не качество
   ответов или производительность большой модели.

Одноразовый HTTP API-ключ не записывается в файлы: curl получает конфигурацию
через stdin, а `llama-server` — через поддерживаемую `LLAMA_API_KEY` в окружении
дочернего процесса. Ключ передаётся гостю по stdin ADB с выключенным TTY,
поэтому не попадает в аргументы команд и не отображается эхом терминала.
Временные SSH-ключи по-прежнему защищены правами доступа и удаляются при cleanup.

Сохраняются версии, ELF-отчёт, inventory, журналы контейнера и тестовых серверов,
logcat, результат upstream-теста и доступный `host_bugreport.zip` с диагностикой
загрузки. Диагностика собирается и при ошибке. Временные приватные ключи и API-токен
не включаются в отчёт. Orchestrator слушает только `127.0.0.1` runner; ADB работает
внутри контейнера, без опубликованного порта. SSH и HTTP проверяются через ADB
forward внутри контейнера. Секреты репозитория и USB телефона не используются.

Тест не вызывает `adb root`, `remount`, `setenforce`, остановку Zygote или изменения
системных служб. Завершает только запущенные им тестовые процессы. OpenSSH использует
поддерживаемый режим сертификатов `TrustedUserCAKeys`: обычный `authorized_keys`
под `/data/local/tmp` конфликтует с проверкой прав родительского каталога.
Проверка строгих прав не отключается. Этот непривилегированный тест не задаёт
конфигурацию будущей системной SSH-службы Cerro.

Это не полная сборка Android: Bazel собирает небольшой host-тест и получает готовые
образы. Upstream-тест создаёт VM с 8 GiB RAM и 8 виртуальными CPU. На временном
GitHub runner освобождаются неиспользуемые Android SDK и .NET SDK, затем требуется
не менее 20 GiB свободного диска. Контейнер удаляется после сбора диагностики.
Перезагрузка хоста или телефона не нужна.

## Закреплённые входы

| Вход | Версия |
| --- | --- |
| Upstream test | `google/android-cuttlefish` commit `a269362bbc0850a04ebffda92fe2219c1bc6f7af` |
| Официальный контейнер | `1.57.13`, manifest SHA-256 `556357126cb99e4caa4842e7892051257f3bd9d41fa74b46ea158f044e765d7a` |
| Android target | `aosp_cf_x86_64_only_phone-userdebug` |
| Android build | `15150359`, одинаковый для image и `cvd-host_package.tar.gz` |
| Bazel / Go | `8.5.1` / `1.24.12`, заданы upstream-тестом |
| llama.cpp / NDK / Android API | [`server/versions.json`](versions.json), общие с ARM64 kit |
| OpenSSH / OpenSSL | `10.6p1` / `3.5.9`, commit SHA в [`virtual/versions.json`](virtual/versions.json) |
| Тестовая модель | `ggml-org/models`, `tinyllamas/stories260K.gguf`, 1 185 376 байт; revision и SHA-256 в `virtual/versions.json` |

SHA-256 Android-архивов отдельно не закреплены: upstream скачивает их по HTTPS
из фиксированного build ID. Недоступность исходных артефактов должна давать ошибку;
автоматического перехода на `latest` нет.

Модель загружается по закреплённому revision с проверкой размера и SHA-256,
не включается в комплект бинарников и не подменяется другой при ошибке загрузки.

## Проверка без Zygote пока не выполнена

8 ноября 2026 года проверены доступные варианты штатного нативного образа AOSP.
Minidroid имеет собственный init без запуска Zygote/`system_server`, но image и
соответствующий host package найденных сборок `10448730` и `10897552` возвращали
HTTP 404 из хранилища. Пригодного готового образа нового SDV Core также не найдено.
Подходящего Android build tree в рабочей среде нет. Поэтому текущий workflow
использует доступный стандартный образ и явно сохраняет наличие framework.

Следующий отдельный шаг — собрать виртуальный Cuttlefish product, использующий
общую с Cerro конфигурацию нативного сервера. Физический product Cerro нельзя
загрузить как обычный образ Cuttlefish: у него другой kernel/vendor hardware.
Для сборки потребуется подходящий Android build tree и отдельные ресурсы.

Для такого образа нужны проверки отсутствия процессов Zygote/`system_server`
после холодной загрузки и повторение проверок данных, SSH и inference. Простое
наличие `ro.zygote` не доказывает, что процесс запущен: у Minidroid отдельный init.
Ожидание framework-specific `sys.boot_completed` и `inventory.sh` с вызовом `pm`
нельзя автоматически переносить на произвольный нативный product. Готовность
launcher и сервисов нужно проверять по контракту выбранного образа, с ограниченным
временем ожидания, без подделки boot properties.

Успех виртуального теста не подтвердит Wi-Fi Qualcomm, UFS/KeyMint, USB gadget,
зарядку, thermal management и ускорители NX721J: они требуют аппаратных испытаний.

## Основания

- [Официальный GitHub workflow Cuttlefish](https://github.com/google/android-cuttlefish/blob/a269362bbc0850a04ebffda92fe2219c1bc6f7af/.github/workflows/presubmit.yaml).
- [Используемый интеграционный тест](https://github.com/google/android-cuttlefish/blob/a269362bbc0850a04ebffda92fe2219c1bc6f7af/e2etests/orchestration/create_from_images_zip_test/main_test.go) и [его Android-артефакты](https://github.com/google/android-cuttlefish/blob/a269362bbc0850a04ebffda92fe2219c1bc6f7af/e2etests/orchestration/artifacts/BUILD.bazel).
- [Официальные контейнеры](https://github.com/google/android-cuttlefish/blob/a269362bbc0850a04ebffda92fe2219c1bc6f7af/container/README.md).
- [Требования Cuttlefish к KVM и соответствию image/host package](https://source.android.com/docs/devices/cuttlefish/get-started).
- [Ограничения GitHub-hosted runners](https://docs.github.com/en/actions/concepts/runners/github-hosted-runners): вложенная виртуализация возможна, но общая гарантия её поддержки не предоставляется.
- [Minidroid init](https://android.googlesource.com/device/google/cuttlefish/+/a1162ca7a4e6297f1699b65052a8c2dd466fd518/shared/minidroid/init.rc) и [BoardConfig](https://android.googlesource.com/device/google/cuttlefish/+/a1162ca7a4e6297f1699b65052a8c2dd466fd518/shared/minidroid/BoardConfig.mk).
- [Штатная сборка SDV Core](https://source.android.com/docs/automotive/sdv/getting-started/build).
- [OpenSSH portable](https://github.com/openssh/openssh-portable/tree/a8c72b842423f0774f542d3c2197247c3dd28d74) и [сборка OpenSSL для Android](https://github.com/openssl/openssl/blob/45e844fa2a14ec92d146bd8f5778ac130b6625fb/NOTES-ANDROID.md).
