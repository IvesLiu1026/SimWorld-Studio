import React from "react";
import { Btn, ModalFooter, ModalHeader, ModalOverlay } from "../../components/ui/primitives.jsx";

export default function ConfirmDeleteModal({ message, onCancel, onConfirm }) {
  return (
    <ModalOverlay onClose={onCancel} maxWidth={420}>
      <ModalHeader title="Confirm Delete" onClose={onCancel} />
      <div style={{ padding: "16px 20px", color: "var(--ink-3)", fontSize: 13, lineHeight: 1.5 }}>
        {message}
      </div>
      <ModalFooter>
        <Btn variant="cancel" onClick={onCancel}>
          Cancel
        </Btn>
        <Btn variant="danger" onClick={onConfirm}>
          Delete
        </Btn>
      </ModalFooter>
    </ModalOverlay>
  );
}
