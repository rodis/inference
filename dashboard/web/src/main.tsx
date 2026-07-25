import React from "react";
import { createRoot } from "react-dom/client";
import App from "./app/App";
import { bootTheme } from "./app/theme";
import "./styles.css";

bootTheme();   // stamp data-theme before the first paint, or an override flashes

createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
