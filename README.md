# Bilibili Cv Downloader

把 Bilibili 专栏文集转换成 EPUB。项目地址：

https://github.com/2936410488/Bilibili-Cv-Downloader

## 功能

- 支持按文集 ID 批量抓取 CV 专栏文章。
- 生成带目录的 EPUB 文件。
- 缓存已抓取的文章 HTML 和图片，重复运行时减少网络请求。
- 自动下载正文图片、文集封面和每章 banner 图，重复 banner 只写入一份。
- 支持新版页面的 `window.__INITIAL_STATE__` 正文结构。
- 文章和图片按顺序抓取，并在网络请求之间间隔 1 秒，减少触发限流的风险。

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

也可以通过配置文件运行：

```bash
python converter.py -c config.ini
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

也可以在 `config.ini` 里指定 Cookie 文件位置和文集 ID：

```ini
[converter]
readlist_id = 702577
cookie_file = ./cookies.txt
```

或者直接把 Cookie 按行填进配置文件；如果 `cookies` 有内容，会优先使用这里的内容：

```ini
[converter]
readlist_id = 702577
cookies =
    buvid3=...
    b_nut=...
    SESSDATA=...
```

也支持逐项写到 `[cookies]` 段：

```ini
[cookies]
buvid3 = ...
b_nut = ...
SESSDATA = ...
```

## 缓存

程序会把缓存写入 `.cache/`：

- `.cache/cv_<文章ID>.html`：文章页面缓存
- `.cache/imgs/`：图片缓存

如果怀疑缓存内容过期，可以删除 `.cache/` 后重新运行。
