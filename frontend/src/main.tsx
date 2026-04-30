import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter, Route, Routes, Navigate } from "react-router-dom";
import App from "./App";
import Upload from "./pages/Upload";
import Review from "./pages/Review";
import Inspect from "./pages/Inspect";
import { applyTheme, resolveInitial } from "./theme";
import "./styles.css";

// Apply theme before first paint to avoid flash.
applyTheme(resolveInitial());

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<App />}>
          <Route index element={<Navigate to="/upload" replace />} />
          <Route path="upload" element={<Upload />} />
          <Route path="review/:id" element={<Review />} />
          <Route path="inspect/:id" element={<Inspect />} />
        </Route>
      </Routes>
    </BrowserRouter>
  </React.StrictMode>
);
