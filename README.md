# Replay Buffer

Приложение для Windows: постоянно записывает экран и звук в кольцевой буфер, а по горячей клавише сохраняет последние секунды в клип.

- По умолчанию **Alt + F10** сохраняет последние **60 секунд**.
- Системный звук, микрофон и отдельная дорожка Discord.
- Настройка монитора, FPS, разрешения и качества.
- Кодирование NVIDIA NVENC, AMD AMF и резервный вариант через CPU.
- Автоклипы CS2 через Game State Integration.

## Подготовка

Нужны Windows, Python 3.12, Microsoft Edge WebView2 Runtime и .NET 8 SDK для сборки помощника захвата звука. Захват звука отдельных процессов требует поддержки WASAPI process loopback в установленной версии Windows.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Отдельно установите Windows-сборку FFmpeg с фильтром `ddagrab` и кодировщиком `libx264`. Для аппаратного кодирования нужны соответствующие кодировщики NVENC/AMF и драйвер видеокарты. Поместите `ffmpeg.exe` в `bin\ffmpeg.exe`.

Соберите помощник захвата звука:

```powershell
dotnet build proccap -c Release -o proccap\out
New-Item -ItemType Directory -Force bin\proccap
Copy-Item proccap\out\proccap.* bin\proccap\
python replay.py
```

Приложение работает в системном трее. Клипы по умолчанию сохраняются в `%USERPROFILE%\Videos\Replays`, настройки — в `%APPDATA%\ReplayBuffer`.

## Сборка

После подготовки FFmpeg и помощника захвата звука, в активированном виртуальном окружении:

```powershell
python -m pip install pyinstaller
.\build.bat
```

Результат: `dist\ReplayBuffer\ReplayBuffer.exe`. Для переноса приложения нужна вся папка `dist\ReplayBuffer`, включая `_internal`; для помощника также требуется .NET 8 Runtime.

Готовые сборки, FFmpeg, скомпилированный помощник и локальные настройки не входят в исходники репозитория.
