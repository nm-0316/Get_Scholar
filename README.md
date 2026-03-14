# Google Books PDF取得ツール（Windows向け）


Google Books のURLから、次の優先順でPDFを保存します。


1. **公式PDFダウンロードリンク**がある場合はそのPDFを保存
2. リンクがない場合は、**画面表示できるページ画像を収集してPDF化**


最新版では、Google側の埋め込み `src`（署名付き画像URL）を優先して取得し、
`image not available` のようなプレースホルダ画像を除外するよう改善しています。

## 動作環境
- Windows 10/11
- Python 3.10 以上

## 使い方（Windows）
1. Pythonをインストール
2. このフォルダで以下を実行
   ```powershell
   pip install -r requirements.txt
   ```
3. `run_windows.bat` を実行
4. Google Books のURLを入力して Enter
5. `downloads` フォルダに出力

## 出力ファイル
- 公式PDF取得時: サーバー指定名（なければ `google_books_download.pdf`）
- プレビューPDF化時: `<book_id>_preview_pages.pdf`

## EXE化（任意）
```powershell
pip install pyinstaller
pyinstaller --onefile --name gbooks_pdf_fetcher gbooks_pdf_fetcher.py
```

生成物: `dist\\gbooks_pdf_fetcher.exe`
