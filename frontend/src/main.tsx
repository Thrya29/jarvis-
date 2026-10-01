// JARVIS desktop UI. Everything is rendered as React text nodes - never innerHTML or
// dangerouslySetInnerHTML - so nothing a task produces (file names, web text, email,
// model output) can inject markup. A test enforces this.
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import "./styles.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
