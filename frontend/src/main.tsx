import { render } from "preact";
import { App } from "./app/App";
import { ErrorBoundary } from "./app/ErrorBoundary";
import "./styles/base.css";

render(
  <ErrorBoundary>
    <App />
  </ErrorBoundary>,
  document.getElementById("app")!,
);
