# 本地前端运行依赖

这些文件只用于 ERP 浏览器页面离线加载，版本号已经写入文件名，禁止替换为未锁定版本的 CDN 地址。

| 依赖 | 版本 | npm 来源 | 运行文件 SHA-256 |
| --- | --- | --- | --- |
| Vue | 3.5.40 | `vue@3.5.40/dist/vue.global.prod.js` | `9E0039A3F6ED0E85308E24D737447F1AF6AF83D229D69E1267A32B29BC2A1337` |
| Axios | 1.18.1 | `axios@1.18.1/dist/axios.min.js` | `DE2511864B48B3A371A9C789A9CF624B3BCDE628FB6572B6B936A5CC2C48C26F` |
| pinyin-pro | 3.26.0 | `pinyin-pro@3.26.0/dist/index.js` | `83F5C34BC94B1E4F0E0CA8BE6AB28C095CEA38D8CF70E5FD45E9656B5608B041` |
| Tailwind CSS（来料页专用构建） | 3.4.17 | `tailwindcss@3.4.17` | `77E10624D5E1ACA9B983025D920865969AD59194609643AFAF871AC073156A21` |

同目录保留各依赖随 npm 包发布的许可证文件。更新依赖时必须同时更新版本化文件名、许可证、SHA-256、前端静态测试和隔离 UAT。

## 来料页 Tailwind CSS 重建

`tailwindcss-3.4.17-incoming.min.css` 只扫描 `static/incoming.html`，由仓库内锁定依赖生成，不得手工编辑，也不得改回公网 CDN：

```powershell
Set-Location tools\incoming-tailwind
npm ci --ignore-scripts --no-audit --no-fund
npm run build
```

重建后必须重新计算运行文件的完整 SHA-256，并同步更新本表以及 `static/incoming.html` 中资源 URL 的前 12 位哈希参数。构建依赖、扫描范围和输入样式分别锁定在 `package-lock.json`、`tailwind.config.cjs` 和 `src/incoming.css`。
