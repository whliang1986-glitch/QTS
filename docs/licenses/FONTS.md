# 字体登记（H-01）

## 现状

客户端不打包任何字体文件，也不引用外部字体服务（Google Fonts、Typekit 等）。
界面用系统字体栈，定义在 `web/src/styles.css` 的 `--font` 变量：

`"Segoe UI", "Microsoft YaHei UI", "PingFang SC", "Noto Sans CJK SC", system-ui, sans-serif`

这些字体由用户电脑的操作系统提供，我们不分发字体文件，所以不涉及字体的商用授权。

## 规则（`tests/arch/test_fonts.py` 强制）

- 仓库里出现字体文件（`.woff`、`.woff2`、`.ttf`、`.otf`、`.eot`、`.ttc`、`.otc`、`.pfb`），必须登记在下表里，否则测试失败。
- 网页源码里出现 `@font-face`、`new FontFace(`、`document.fonts.add`、`@fontsource`、外部字体服务的地址，
  或者任何指向字体文件的 http(s) 地址，测试一律失败。要自带字体时，字体文件放进仓库并登记，
  不从外部服务加载（外部服务会泄露访问记录，断网时也加载不了）。
- 许可只允许可免费商用、允许再分发的三种：SIL Open Font License（OFL-1.1）、Apache-2.0、MIT。
  其他许可（包括各种“个人免费”字体）一律不用，确有需要先单独评估。
- 登记时写明来源网址和许可文件在仓库里的位置。
- 检查只看已入库（git 跟踪）的文件：CI 上覆盖全部改动；本地新建但还没 `git add` 的文件要等加入后才会被检查。

## 已登记的字体

| 文件 | 许可 | 来源 | 许可文件 |
|---|---|---|---|
