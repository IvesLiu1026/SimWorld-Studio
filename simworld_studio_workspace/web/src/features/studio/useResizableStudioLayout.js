import { useCallback, useRef, useState } from "react";

function initialColumnWidth() {
  if (typeof window === "undefined") return 360;
  return Math.round(window.innerWidth * 0.28);
}

export function useResizableStudioLayout() {
  const [colLeft, setColLeft] = useState(initialColumnWidth);
  const [colRight, setColRight] = useState(initialColumnWidth);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [drawerTab, setDrawerTab] = useState("assets");
  const [drawerH, setDrawerH] = useState(200);

  const leftColRef = useRef(null);
  const rightColRef = useRef(null);
  const colResizingLeft = useRef(false);
  const colResizingRight = useRef(false);
  const colResizeStart = useRef({ x: 0, colLeft: 390, colRight: 360 });
  const drawerResizing = useRef(false);
  const drawerResizeStart = useRef({ y: 0, h: 200 });
  const layoutRef = useRef(null);

  const startColResize = useCallback(
    (side) => (event) => {
      event.preventDefault();
      if (side === "left") {
        colResizingLeft.current = true;
        colResizeStart.current = { x: event.clientX, colLeft, colRight };
      } else {
        colResizingRight.current = true;
        colResizeStart.current = { x: event.clientX, colLeft, colRight };
      }
      document.body.style.cursor = "col-resize";
      document.body.style.userSelect = "none";

      const onMove = (moveEvent) => {
        const dx = moveEvent.clientX - colResizeStart.current.x;
        if (colResizingLeft.current) {
          setColLeft(Math.max(280, Math.min(600, colResizeStart.current.colLeft + dx)));
        } else if (colResizingRight.current) {
          setColRight(Math.max(260, Math.min(560, colResizeStart.current.colRight - dx)));
        }
      };

      const onUp = () => {
        colResizingLeft.current = false;
        colResizingRight.current = false;
        document.body.style.cursor = "";
        document.body.style.userSelect = "";
        window.removeEventListener("mousemove", onMove);
        window.removeEventListener("mouseup", onUp);
      };

      window.addEventListener("mousemove", onMove);
      window.addEventListener("mouseup", onUp);
    },
    [colLeft, colRight]
  );

  const startDrawerResize = useCallback(
    (event) => {
      event.preventDefault();
      drawerResizing.current = true;
      drawerResizeStart.current = { y: event.clientY, h: drawerH };
      document.body.style.cursor = "row-resize";
      document.body.style.userSelect = "none";
      const pendingH = { value: drawerH };

      const onMove = (moveEvent) => {
        if (!drawerResizing.current) return;
        const delta = drawerResizeStart.current.y - moveEvent.clientY;
        pendingH.value = Math.max(80, Math.min(600, drawerResizeStart.current.h + delta));
        const handle = document.querySelector(".sw-drawer .sw-drawer-handle-preview");
        if (handle) handle.style.transform = `translateY(${-delta}px)`;
      };

      const onUp = () => {
        drawerResizing.current = false;
        document.body.style.cursor = "";
        document.body.style.userSelect = "";
        setDrawerH(pendingH.value);
        if (pendingH.value > 50) setDrawerOpen(true);
        window.removeEventListener("mousemove", onMove);
        window.removeEventListener("mouseup", onUp);
      };

      window.addEventListener("mousemove", onMove);
      window.addEventListener("mouseup", onUp);
    },
    [drawerH]
  );

  return {
    colLeft,
    colRight,
    drawerH,
    drawerOpen,
    drawerTab,
    layoutRef,
    leftColRef,
    rightColRef,
    setDrawerH,
    setDrawerOpen,
    setDrawerTab,
    startColResize,
    startDrawerResize,
  };
}
