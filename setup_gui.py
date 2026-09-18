import tkinter as tk
from tkinter import messagebox


def run_setup_dialog():
    """Ventana emergente para activar el agente: solo Client ID + API Key (de la
    EMPRESA, una sola para todas sus minas, estilo Foreman). El servidor es interno del
    programa, el usuario no lo ve ni lo configura. El nombre de la mina NO se pide aqui -
    se le pone/cambia despues desde el programa central (pestana Agentes), para poder
    renombrarla sin tener que volver a tocar esta PC.
    Devuelve (client_id, api_key) o None si se cerro la ventana."""
    result = {}

    root = tk.Tk()
    root.title("BiTeam ERP - Activacion de agente")
    root.resizable(False, False)

    padding = {"padx": 16, "pady": 6}

    tk.Label(root, text="Activar agente", font=("Segoe UI", 13, "bold")).grid(
        row=0, column=0, columnspan=2, padx=16, pady=(16, 4), sticky="w"
    )
    tk.Label(
        root,
        text="Ingresa el Client ID y API Key de tu empresa (pestana Agentes\n"
             "del programa central). El nombre de esta mina se lo pones despues\n"
             "desde ahi mismo, en cuanto este agente aparezca en la lista.",
        justify="left", fg="#555",
    ).grid(row=1, column=0, columnspan=2, padx=16, pady=(0, 10), sticky="w")

    tk.Label(root, text="Client ID:").grid(row=2, column=0, sticky="e", **padding)
    client_id_entry = tk.Entry(root, width=34)
    client_id_entry.grid(row=2, column=1, **padding)

    tk.Label(root, text="API Key:").grid(row=3, column=0, sticky="e", **padding)
    api_key_entry = tk.Entry(root, width=34)
    api_key_entry.grid(row=3, column=1, **padding)

    def on_start():
        client_id = client_id_entry.get().strip()
        api_key = api_key_entry.get().strip()
        if not client_id or not api_key:
            messagebox.showerror("Faltan datos", "Ingresa el Client ID y el API Key.")
            return
        result["client_id"] = client_id
        result["api_key"] = api_key
        root.destroy()

    start_btn = tk.Button(root, text="Iniciar", width=14, command=on_start, bg="#4f8cff", fg="white")
    start_btn.grid(row=4, column=0, columnspan=2, pady=(6, 16))

    root.bind("<Return>", lambda _e: on_start())
    client_id_entry.focus_set()
    root.eval("tk::PlaceWindow . center")
    root.mainloop()

    if not result:
        return None
    return result["client_id"], result["api_key"]
