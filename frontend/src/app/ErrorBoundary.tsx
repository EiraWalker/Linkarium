import { Component, type ComponentChildren } from "preact";
export class ErrorBoundary extends Component<
  { children: ComponentChildren },
  { failed: boolean }
> {
  state = { failed: false };
  static getDerivedStateFromError() {
    return { failed: true };
  }
  render() {
    return this.state.failed ? (
      <main style={{ padding: "48px 24px", maxWidth: "600px", margin: "auto" }}>
        <h1>页面暂时无法显示</h1>
        <p>请重新载入页面。已经保存在服务器上的网站与快照不会丢失。</p>
        <button onClick={() => location.reload()}>重新载入</button>
      </main>
    ) : (
      this.props.children
    );
  }
}
