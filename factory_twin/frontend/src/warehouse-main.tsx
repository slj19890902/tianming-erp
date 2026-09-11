import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { WarehouseTwinApp } from "./WarehouseTwinApp";
import "./styles.css";
import "./warehouseTwin.css";
import "./warehouseWorkspace.css";

createRoot(document.getElementById("warehouse-twin-root")!).render(
  <StrictMode>
    <WarehouseTwinApp />
  </StrictMode>
);
