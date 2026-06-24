# Go2 Audio Recorder

`unitree_webrtc_connect/examples/go2/audio/save_audio/save_audio_to_file.py` を元にした、簡易 Flask 録音アプリです。

この開発 PC のように実機用の WebRTC/音声ライブラリが入っていない環境でも、Web 画面だけは起動できます。録音を開始した時点で `unitree_webrtc_connect` や `numpy` が不足している場合は、画面にエラーを表示します。

## 起動

PowerShell では、このフォルダから venv を有効化して起動します。

```powershell
..\..\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python app.py
```

`Activate.ps1` が実行ポリシーで止まる場合は、同じ venv を `activate.bat` から使えます。

```powershell
cmd /c "..\..\Scripts\activate.bat && python -m pip install -r requirements.txt && python app.py"
```

ブラウザで `http://127.0.0.1:5000` を開きます。

## 録音

実機を動かす PC では、隣の `unitree_webrtc_connect` とその依存関係が使える状態にしてから録音してください。既定では `../unitree_webrtc_connect` を Python パスへ追加します。場所が違う場合は `UNITREE_WEBRTC_REPO` にリポジトリパスを指定します。

## 環境変数

- `UNITREE_GO2_IP`: 画面に入る初期 IP アドレス
- `UNITREE_WEBRTC_REPO`: `unitree_webrtc_connect` リポジトリのパス
- `UNITREE_RECORDINGS_DIR`: WAV 保存先
- `FLASK_HOST`, `FLASK_PORT`: Flask の待ち受け設定
