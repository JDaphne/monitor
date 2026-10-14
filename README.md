# monitor

中国银行 2027 全球校园招聘统一考试 —— **纽约考位监控 & 自动预约**脚本。

预约时间：北京时间 2026-10-14 09:00 ~ 2026-10-16 11:00
系统地址：https://boc2027xzks.campustest.cn/

## 安装

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

## 运行

```bash
# macOS / Linux
export BOC_USER="E12345678"   # 网申证件号码：字母大写、去掉括号
export BOC_PASS="123456"      # 网申联系电话后 6 位
python boc_monitor.py

# Windows PowerShell
$env:BOC_USER="E12345678"; $env:BOC_PASS="123456"; python boc_monitor.py
```

运行后会打开一个浏览器窗口：

1. 自动填写账号密码；**如果有验证码，请在窗口里手动输入并点登录**，脚本会等你。
2. 自动尝试点击“考位预约”入口；如果没点进去，**手动点进选择考点/考位的页面**即可。
3. 之后每约 20 秒刷新一次，检查含“纽约 / New York”且未满的考位；一旦有位，
   立即点击“预约/选择”和后续的“确定/确认/提交”，终端响铃并截图到 `debug/`。

常用参数：

| 参数 | 说明 |
| --- | --- |
| `--dry-run` | 只提醒、不自动点击预约（建议先用这个模式试跑一次） |
| `--interval 15` | 刷新间隔秒数（请勿设得太小，以免被风控） |
| `--booking-url URL` | 直接指定预约页地址 |
| `--keywords "纽约,New York"` | 考点关键字 |
| `--notify-url "https://api.day.app/<你的key>/{msg}"` | 手机推送（Bark / Server 酱等任意 GET 地址） |
| `--end "2026-10-16 11:00"` | 截止时间（按本机时区；非北京时间请设 `TZ=Asia/Shanghai`） |

## 注意

- 我无法事先访问该系统的页面，脚本对页面结构做了通用的推断（下拉框选城市、表格/卡片中的
  “预约”按钮、“已满”字样等）。如果运行不符合预期，请把 `debug/` 目录下的 HTML、截图以及
  `api_log.jsonl` 发过来，可以据此精确调整。
- 登录状态保存在 `.browser_profile/`，重启脚本通常无需重新登录。
- 自动预约后请务必在系统中亲自核对预约结果。
