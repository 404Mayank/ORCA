import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import "./index.css";
import "maplibre-gl/dist/maplibre-gl.css";
import { str } from "./i18n/strings";
import { loadTheme } from "./storage";

// Paint the stored theme before first render: otherwise a dark-theme user
// gets one light frame (the boot screen follows the tokens). Single
// source with storage.ts -- no duplicated key or default here.
document.documentElement.dataset.theme = loadTheme();
document.title = str.meta.documentTitle;

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
