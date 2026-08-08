# .github

kouiso の全リポジトリで共通して使う Issue / PR テンプレートを置いています。

GitHub の既定コミュニティヘルスファイルの仕組みにより、ここに置いたテンプレートは
自前のテンプレートを持たないリポジトリすべてで自動的に使われます。
文面を直すときはこのリポジトリだけを直せば、全リポジトリに反映されます。

## 中身

| ファイル | 用途 |
| --- | --- |
| `.github/ISSUE_TEMPLATE/bug.yml` | バグ修正・機能改修 (PBI) |
| `.github/ISSUE_TEMPLATE/feature.yml` | 新規開発・機能改修 (PBI) |
| `.github/ISSUE_TEMPLATE/sbi.yml` | PBI を実装単位に分解する SBI |
| `.github/ISSUE_TEMPLATE/config.yml` | テンプレートを使わない空 Issue を無効化 |
| `.github/pull_request_template.md` | PR の説明文 |

## 個別リポジトリで上書きしたいとき

そのリポジトリに `.github/ISSUE_TEMPLATE/` を作ると、ここの Issue テンプレートは
**1 つ残らず**使われなくなります。一部だけ差し替えることはできません。
上書きする場合は 4 ファイルすべてを置いてください。

## SBI の親子関係について

SBI の本文に親 Issue の番号は書きません。GitHub には本文の記法で親子を繋ぐ仕組みが
無いためです。次のどちらかで作ると、正式なサブ Issue として親に紐づきます。

- 親 Issue 下部の **Create sub-issue**
- `gh issue create --parent <親issue番号>`
