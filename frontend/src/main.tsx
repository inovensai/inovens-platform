import React from "react";
import ReactDOM from "react-dom/client";
import { FluentProvider, createDarkTheme } from "@fluentui/react-components";
import App from "./App";
import "./styles.css";

const inovensTheme = {
  ...createDarkTheme({
    10: "#071a12", 20: "#0c2d1f", 30: "#12432e", 40: "#175a3e",
    50: "#1c714e", 60: "#21885e", 70: "#269f6e", 80: "#2db77f",
    90: "#39ca90", 100: "#54d49f", 110: "#70ddae", 120: "#8be5bd",
    130: "#a7edcc", 140: "#c2f4db", 150: "#def9ea", 160: "#f2fdf7"
  }),
  fontFamilyBase: "Aptos, Segoe UI, system-ui, sans-serif",
  borderRadiusMedium: "10px",
  borderRadiusLarge: "14px"
};

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode><FluentProvider theme={inovensTheme}><App /></FluentProvider></React.StrictMode>
);
