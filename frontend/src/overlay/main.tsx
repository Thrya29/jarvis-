// The floating overlay's page (shown by jarvis-overlay.exe). Text-only rendering, like
// the main window; it never receives native (IPC) access.
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { Overlay } from "./Overlay";
import "./overlay.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <Overlay />
  </StrictMode>,
);
