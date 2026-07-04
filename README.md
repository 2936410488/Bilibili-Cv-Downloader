# Bilibili Cv Downloader

把 Bilibili 专栏文集转换成 EPUB。项目地址：

https://github.com/2936410488/Bilibili-Cv-Downloader

## 功能

- 支持按文集 ID 批量抓取 CV 专栏文章。
- 生成带目录的 EPUB 文件。
- 缓存已抓取的文章 HTML 和图片，重复运行时减少网络请求。
- 自动下载正文图片、文集封面和每章 banner 图。
- 支持新版页面的 `window.__INITIAL_STATE__` 正文结构。
- 支持文章和图片并发抓取。

## 安装

```bash
git clone https://github.com/2936410488/Bilibili-Cv-Downloader.git
cd Bilibili-Cv-Downloader
pip install -r requirements.txt
```

建议使用 Python 3.10 或更新版本。

## 使用

```bash
python converter.py <文集ID>
```

例如：

```bash
python converter.py 702577
```

生成的 EPUB 会保存在当前目录，文件名使用文集标题。

默认文章并发数为 `4`，图片并发下载数为 `8`。如果遇到限流或请求失败，可以降低并发：

```bash
python converter.py 702577 --workers 1 --image-workers 2
```

查看全部参数：

```bash
python converter.py --help
```

## Cookies

如果部分文章需要登录访问，请把浏览器里的 Bilibili Cookie 粘贴到项目根目录的 `cookies.txt` 文件中。

格式示例：

```text
buvid3=...; b_nut=...; b_lsid=...; _uuid=...
```

## 缓存

程序会把缓存写入 `.cache/`：

- `.cache/cv_<文章ID>.html`：文章页面缓存
- `.cache/imgs/`：图片缓存

如果怀疑缓存内容过期，可以删除 `.cache/` 后重新运行。
