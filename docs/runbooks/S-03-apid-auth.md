# 运行手册：apid 令牌与 TLS（S-03）

## 令牌

### 签发与列出

命令行要拿 api.db 的单写者锁，而 apid 运行时一直持有这把锁，所以先要停 apid。签发会写 api.db，列出只读，但也要拿锁。

目前守护进程不支持单独停 apid：停守护进程会连同 tradingd 一起停。日常签发请安排在休市后。急需签发时可以单独运行
`python -m server.app apid`，但这样做要先停守护进程，所以仍须在休市后。紧急情况只做吊销，吊销见下文，不用停任何进程。

```
python -m server.app token issue --name dylan --role owner --days 90
python -m server.app token list
```

- 明文令牌只显示一次，请保存到密码管理器。库里只存哈希，丢了只能吊销后重发。
- 有效期最长 365 天，建议 90 天。`list` 会显示每个令牌是“有效”“已过期”还是“已吊销”。
- 角色：owner 可以发命令；viewer 只能查看。

### 吊销

紧急吊销不用停任何进程。用一个 owner 令牌调用：

```
POST /v1/tokens/<令牌编号>/revoke   {"reason": "疑似泄露"}
```

吊销立即生效，也可以吊销自己当前用的令牌。之后在休市后停 apid，签发新令牌。
注意：拿到 owner 令牌的人也能吊销所有令牌，把你挡在外面。恢复方法是停 apid，用命令行重新签发，再吊销可疑的令牌。

apid 停着的时候，也可以用命令行吊销：

```
python -m server.app token revoke --id <令牌编号> --reason "换新"
```

认证失败、限流、签发、吊销、二次确认、命令提交与结果，都会写进 api.db 的审计表。审计里不记录令牌明文和确认短语。

## 高危命令的两道确认

高危命令包括：撤销保护性止损、紧急平仓、采用券商持仓为基准。

1. 客户端先申请二次确认（`/v1/commands/confirmations`），拿到一个 60 秒内有效的确认号。
2. 提交命令时，同时带上这个确认号和 tradingd 给出的确认短语。

两道确认相互独立，缺一道命令都不会执行。同一个 request_id 重试时，可以用同一个确认号。

## 局域网访问与 TLS

- **只在本机使用**：保持 `config/runtime/apid.toml` 里的 `host = "127.0.0.1"`，不需要证书。
  只认字面的回环 IP（127.0.0.1、::1），`localhost` 这类主机名不算。
- **从局域网访问**：
  1. 在服务器上生成自签证书，证书里写明服务器的局域网 IP：

     ```
     openssl req -x509 -newkey rsa:3072 -sha256 -days 365 -nodes -keyout apid-key.pem -out apid-cert.pem -subj "/CN=qts-apid" -addext "subjectAltName=IP:<服务器局域网IP>"
     ```

  2. 把两个文件放到仓库根目录下的 `secrets/apid/`，只给运行 apid 的系统账户读权限。
     `.gitignore` 已经忽略 `secrets/`、`*.pem` 和 `*.key`，它们不会进入版本库，也不会进入备份（S-08）。
  3. 在 `apid.toml` 里把 `host` 改成服务器的局域网地址，并把 `tls_cert`、`tls_key` 填成上面两个文件的相对路径。
     配置路径只能写仓库根目录下的相对路径。文件缺失或加载失败时，apid 拒绝启动（退出码 78，守护进程不会反复拉起它）。
  4. 客户端固定这张证书（记下证书指纹），不接受其他证书。
- **不支持反向代理**：apid 不信任任何转发头（如 X-Forwarded-For），限流和审计只看 TCP 连接的对端地址。
  如果客户端在 NAT 或代理后面，它们会被当成同一个来源一起计数。
- **不要做端口映射，不要暴露到公网。** 在外面时，用 VPN（WireGuard 或 Tailscale）回到局域网。
- 服务器防火墙只对局域网放行 apid 端口。TWS / IB Gateway 的 API 端口只绑定 127.0.0.1（B-03a 的连接规则强制执行，有回归测试）。
