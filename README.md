# Nubia Z60 Ultra: Cerro

Дерево устройства **NX721J / PQ83A01 / cerro**, Snapdragon SM8650.
Основа: LineageOS `lineage-23.2`, коммит `a7ed9a2ab78485804ed384c3fae089a3b291494b`.

Цель — **минимальный нативный сервер на телефоне с управлением через SSH/ADB**;
состав отдельной системы описан в [HEADLESS.md](server/HEADLESS.md).
Текущий готовый этап — **CPU-сервер inference для уже установленного Android**.
GitHub Actions собирает `llama-server` и `llama-bench` под Android arm64; модель GGUF выбирается отдельно.
Управление — через ADB или SSH в Termux. Wi-Fi настраивается в обычных настройках телефона.

- [Сборка и запуск серверного набора](server/README.md).
- [SSH и отдельная установка в Termux](server/termux/README.md).
- [Прямой SSH по USB или домашней сети](server/termux/CONSOLE.md).
- [Deployment из GitHub Actions через домашний runner](server/termux/DEPLOY.md).
- [Задание локальному агенту для настройки телефона](server/termux/LOCAL_AGENT.md).
- [GitHub Actions](https://github.com/qqrm/android_device_nubia_cerro/actions/workflows/server-kit.yml): `Build minimal Android server`.
- [Управление телефоном](https://github.com/qqrm/android_device_nubia_cerro/actions/workflows/deploy-phone.yml): `Deploy to Cerro phone`.

Набор работает поверх существующей прошивки. Это исходный CPU-вариант для замеров;
работа на NX721J, выключенный экран, автозапуск и GPU/NPU требуют проверки на устройстве.
