# Публикация на GitHub

Поскольку подключённый GitHub-коннектор умеет записывать файлы в **существующий** репозиторий, но не создавать новый, понадобится одно действие владельца:

1. Войдите в GitHub как `deker1176` и откройте https://github.com/new.
2. Название: `1c-backup-manager` (или другое по желанию).
3. Укажите **Private** (рекомендовано). Не добавляйте автоматически README/.gitignore/License: все они подготовлены в исходниках, а лицензию можно выбрать позднее.
4. Создайте репозиторий и пришлите его ссылку в ChatGPT: так можно выгрузить исходники через подключённый GitHub.

Альтернативно, можно загрузить весь проект самостоятельно через `git` после создания репозитория:

```powershell
git init -b main
git add .
git commit -m "Initial source release v4.8"
git remote add origin https://github.com/deker1176/1c-backup-manager.git
git push -u origin main
```

**Windows EXE** собирается через GitHub Actions. После загрузки файлов:

1. Откройте **Actions → Build Windows EXE**.
2. При успешном запуске скачайте артефакт `1CBackupManager-windows` (внутри EXE и SHA256SUMS).
3. Чтобы выпустить программу на вкладке **Releases**, нажмите **Run workflow**, отметьте **Publish GitHub Release** и задайте тег `v4.8.0`.
4. Дождитесь успешной проверки и сборки. Только после этого в Releases появится готовый EXE.

В репозиторий **не нужно** выгружать рабочие `config.json`, `secrets.dat`, `scheduler_state.json`, базы `.dt`, `.bak` и пароли API.
