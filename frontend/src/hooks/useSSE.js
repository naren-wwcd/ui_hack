import { useEffect, useRef, useState } from "react";
import { eventStreamUrl } from "../api.js";

/** One shared EventSource with browser-native reconnect behavior. */
export function useSSE(onEvent) {
  const callbackRef = useRef(onEvent);
  const [connection, setConnection] = useState("connecting");

  useEffect(() => {
    callbackRef.current = onEvent;
  }, [onEvent]);

  useEffect(() => {
    let disposed = false;
    let source;
    let retryTimer;

    function connect() {
      if (disposed) return;
      setConnection((current) => current === "connected" ? "reconnecting" : "connecting");

      source = new EventSource(eventStreamUrl());
      source.onopen = () => {
        if (!disposed) setConnection("connected");
      };
      source.onmessage = (message) => {
        try {
          callbackRef.current?.(JSON.parse(message.data));
        } catch (error) {
          console.warn("Ignored malformed AutoHeal event", error);
        }
      };
      source.onerror = () => {
        if (disposed) return;
        setConnection("reconnecting");
        source?.close();
        retryTimer = window.setTimeout(connect, 1500);
      };
    }

    connect();
    return () => {
      disposed = true;
      window.clearTimeout(retryTimer);
      source?.close();
      setConnection("disconnected");
    };
  }, []);

  return connection;
}