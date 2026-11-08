# Cuttlefish в GitHub Actions

Workflow [`Cuttlefish stock-image smoke`](../.github/workflows/cuttlefish-smoke.yml)
подготавливает виртуальный Android на GitHub-hosted `ubuntu-24.04`. Он запускается
вручную и на pull request в `server-lite`, который меняет сам workflow или
`server/device/inventory.sh`. Runner рядом с телефоном не нужен.

**Это проверка стандартного Android-образа и инвентаризации. Она не проверяет
`lineage_cerro`, отдельный продукт без Zygote, SSH или inference. Первый успешный
прогон этого workflow в нашем репозитории пока не подтверждён.**

## Что проверяет job

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

Сохраняются версии, inventory, журналы контейнера, logcat, результат upstream-теста
и доступный `host_bugreport.zip` с диагностикой загрузки. Диагностика собирается
и при ошибке. Orchestrator слушает только `127.0.0.1` runner; ADB работает внутри
контейнера, без опубликованного порта. Секреты и USB телефона не используются.

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

SHA-256 Android-архивов отдельно не закреплены: upstream скачивает их по HTTPS
из фиксированного build ID. Недоступность исходных артефактов должна давать ошибку;
автоматического перехода на `latest` нет.

## Переход к проверке без Zygote

Следующий отдельный шаг — собрать виртуальный Cuttlefish product, использующий
общую с Cerro конфигурацию нативного сервера. Физический product Cerro нельзя
загрузить как обычный образ Cuttlefish: у него другой kernel/vendor hardware.
Для сборки потребуется подходящий Android build tree и отдельные ресурсы.

Для такого образа нужны проверки отсутствия Zygote/`system_server`, доступности
данных, SSH-входа по ключу и реального запроса к inference-серверу. Текущий ARM64
inference kit необходимо отдельно собрать под архитектуру виртуального гостя.
Ожидание Android `sys.boot_completed` и `inventory.sh` с вызовом `pm` в этот тест
переносить нельзя. Готовность launcher и сервисов нужно проверять отдельно,
с ограниченным временем ожидания, без подделки boot properties.

Успех виртуального теста не подтвердит Wi-Fi Qualcomm, UFS/KeyMint, USB gadget,
зарядку, thermal management и ускорители NX721J: они требуют аппаратных испытаний.

## Основания

- [Официальный GitHub workflow Cuttlefish](https://github.com/google/android-cuttlefish/blob/a269362bbc0850a04ebffda92fe2219c1bc6f7af/.github/workflows/presubmit.yaml).
- [Используемый интеграционный тест](https://github.com/google/android-cuttlefish/blob/a269362bbc0850a04ebffda92fe2219c1bc6f7af/e2etests/orchestration/create_from_images_zip_test/main_test.go) и [его Android-артефакты](https://github.com/google/android-cuttlefish/blob/a269362bbc0850a04ebffda92fe2219c1bc6f7af/e2etests/orchestration/artifacts/BUILD.bazel).
- [Официальные контейнеры](https://github.com/google/android-cuttlefish/blob/a269362bbc0850a04ebffda92fe2219c1bc6f7af/container/README.md).
- [Требования Cuttlefish к KVM и соответствию image/host package](https://source.android.com/docs/devices/cuttlefish/get-started).
- [Ограничения GitHub-hosted runners](https://docs.github.com/en/actions/concepts/runners/github-hosted-runners): вложенная виртуализация возможна, но общая гарантия её поддержки не предоставляется.
