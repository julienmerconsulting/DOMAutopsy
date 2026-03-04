"""
QA Scenario Builder — GUI PySide6
===================================
Constructeur de scénarios de test pour QA Explorer.
Chaque étape = action + paramètres dynamiques selon le type d'action.
Export JSON compatible avec qa_explorer.py

JMer Consulting 2026
"""

import sys
import json
import os
from datetime import datetime
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QComboBox, QPushButton, QScrollArea,
    QFrame, QFileDialog, QMessageBox, QTextEdit, QGroupBox,
    QSplitter, QSizePolicy, QToolButton, QSpacerItem
)
from PySide6.QtCore import Qt, Signal, QSize
from PySide6.QtGui import QFont, QIcon, QAction, QColor, QPalette


# ============================================================
# DEFINITIONS DES ACTIONS ET PARAMETRES
# ============================================================

# Chaque action a ses parametres nommes avec type et placeholder
ACTIONS = {
    "navigate": {
        "label": "Naviguer",
        "icon": "🌐",
        "description": "Ouvre une URL dans le navigateur",
        "params": {
            "url": {"label": "URL", "type": "text", "placeholder": "https://example.com/page", "required": True}
        }
    },
    "click": {
        "label": "Cliquer",
        "icon": "🖱️",
        "description": "Clique sur un element",
        "params": {
            "target": {"label": "Cible", "type": "text", "placeholder": "bouton Connexion, lien Panier, icone loupe...", "required": True},
            "wait_before": {"label": "Attente avant (s)", "type": "number", "placeholder": "0", "required": False}
        }
    },
    "input": {
        "label": "Saisir",
        "icon": "⌨️",
        "description": "Tape du texte dans un champ",
        "params": {
            "target": {"label": "Champ cible", "type": "text", "placeholder": "champ email, barre de recherche...", "required": True},
            "value": {"label": "Valeur", "type": "text", "placeholder": "texte a saisir", "required": True},
            "clear_before": {"label": "Vider avant", "type": "bool", "placeholder": "", "required": False}
        }
    },
    "select": {
        "label": "Selectionner",
        "icon": "📋",
        "description": "Selectionne une option dans une liste deroulante",
        "params": {
            "target": {"label": "Liste cible", "type": "text", "placeholder": "liste deroulante Pays, menu Categorie...", "required": True},
            "value": {"label": "Option", "type": "text", "placeholder": "France, Premium, etc.", "required": True}
        }
    },
    "verify": {
        "label": "Verifier",
        "icon": "✅",
        "description": "Verifie qu'un element ou texte est present",
        "params": {
            "target": {"label": "Element attendu", "type": "text", "placeholder": "texte 'Commande confirmee', badge panier = 2...", "required": True},
            "type": {"label": "Type de verif", "type": "choice", "choices": ["presence", "texte_contient", "texte_exact", "visible", "absent"], "required": False}
        }
    },
    "scroll": {
        "label": "Scroller",
        "icon": "📜",
        "description": "Scroll la page",
        "params": {
            "direction": {"label": "Direction", "type": "choice", "choices": ["bas", "haut", "vers_element"], "required": True},
            "target": {"label": "Vers element (si vers_element)", "type": "text", "placeholder": "footer, section Avis...", "required": False}
        }
    },
    "hover": {
        "label": "Survoler",
        "icon": "👆",
        "description": "Survole un element (menu deroulant, tooltip...)",
        "params": {
            "target": {"label": "Element", "type": "text", "placeholder": "menu Compte, bouton Info...", "required": True}
        }
    },
    "wait": {
        "label": "Attendre",
        "icon": "⏳",
        "description": "Attend un certain temps ou qu'un element apparaisse",
        "params": {
            "seconds": {"label": "Duree (s)", "type": "number", "placeholder": "2", "required": False},
            "target": {"label": "Ou attendre element", "type": "text", "placeholder": "spinner disparait, modal se ferme...", "required": False}
        }
    },
    "screenshot": {
        "label": "Capture ecran",
        "icon": "📸",
        "description": "Prend une capture ecran a ce point du scenario",
        "params": {
            "name": {"label": "Nom", "type": "text", "placeholder": "page_accueil, apres_login...", "required": True}
        }
    },
    "cookie": {
        "label": "Accepter cookies",
        "icon": "🍪",
        "description": "Accepte la banniere de cookies si presente",
        "params": {
            "target": {"label": "Bouton (optionnel)", "type": "text", "placeholder": "laisser vide = detection auto", "required": False}
        }
    }
}

# Ordre d'affichage dans le dropdown
ACTION_ORDER = ["click", "input", "verify", "navigate", "select", "scroll", "hover", "wait", "cookie", "screenshot"]


# ============================================================
# WIDGET ETAPE (une ligne du scenario)
# ============================================================

class StepWidget(QFrame):
    """Widget pour une etape du scenario avec parametres dynamiques"""
    
    # Signaux
    delete_requested = Signal(object)
    move_up_requested = Signal(object)
    move_down_requested = Signal(object)
    
    def __init__(self, step_number=1, parent=None):
        super().__init__(parent)
        self.step_number = step_number
        self.param_widgets = {}
        self._setup_ui()
    
    def _setup_ui(self):
        """Construit l'interface de l'etape"""
        self.setFrameStyle(QFrame.Box | QFrame.Raised)
        self.setLineWidth(1)
        self.setStyleSheet("""
            StepWidget {
                background-color: #f8f9fa;
                border: 1px solid #dee2e6;
                border-radius: 6px;
                margin: 2px;
                padding: 4px;
            }
            StepWidget:hover {
                border-color: #0d6efd;
                background-color: #f0f4ff;
            }
        """)
        
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(8, 6, 8, 6)
        main_layout.setSpacing(4)
        
        # --- Ligne 1 : Numero + Action + Boutons ---
        header = QHBoxLayout()
        header.setSpacing(6)
        
        # Numero d'etape
        self.lbl_number = QLabel(f"#{self.step_number}")
        self.lbl_number.setFont(QFont("Consolas", 11, QFont.Bold))
        self.lbl_number.setFixedWidth(35)
        self.lbl_number.setStyleSheet("color: #0d6efd; background: transparent;")
        header.addWidget(self.lbl_number)
        
        # Dropdown action
        self.combo_action = QComboBox()
        self.combo_action.setMinimumWidth(200)
        self.combo_action.setStyleSheet("QComboBox { padding: 4px 8px; font-size: 12px; }")
        for action_id in ACTION_ORDER:
            action = ACTIONS[action_id]
            self.combo_action.addItem(f"{action['icon']} {action['label']}", action_id)
        self.combo_action.currentIndexChanged.connect(self._on_action_changed)
        header.addWidget(self.combo_action)
        
        # Description courte de l'action
        self.lbl_desc = QLabel()
        self.lbl_desc.setStyleSheet("color: #6c757d; font-style: italic; background: transparent;")
        header.addWidget(self.lbl_desc, 1)
        
        # Boutons deplacement / suppression
        self.btn_up = QToolButton()
        self.btn_up.setText("▲")
        self.btn_up.setFixedSize(28, 28)
        self.btn_up.setToolTip("Monter")
        self.btn_up.clicked.connect(lambda: self.move_up_requested.emit(self))
        header.addWidget(self.btn_up)
        
        self.btn_down = QToolButton()
        self.btn_down.setText("▼")
        self.btn_down.setFixedSize(28, 28)
        self.btn_down.setToolTip("Descendre")
        self.btn_down.clicked.connect(lambda: self.move_down_requested.emit(self))
        header.addWidget(self.btn_down)
        
        self.btn_delete = QToolButton()
        self.btn_delete.setText("✕")
        self.btn_delete.setFixedSize(28, 28)
        self.btn_delete.setToolTip("Supprimer cette etape")
        self.btn_delete.setStyleSheet("QToolButton { color: #dc3545; font-weight: bold; } QToolButton:hover { background: #dc3545; color: white; }")
        self.btn_delete.clicked.connect(lambda: self.delete_requested.emit(self))
        header.addWidget(self.btn_delete)
        
        main_layout.addLayout(header)
        
        # --- Zone parametres (dynamique) ---
        self.params_container = QWidget()
        self.params_layout = QHBoxLayout(self.params_container)
        self.params_layout.setContentsMargins(40, 2, 0, 2)
        self.params_layout.setSpacing(10)
        main_layout.addWidget(self.params_container)
        
        # Initialiser les parametres de la premiere action
        self._on_action_changed()
    
    def _on_action_changed(self):
        """Reconstruit les champs parametres quand l'action change"""
        # Vider les anciens parametres
        self.param_widgets.clear()
        while self.params_layout.count():
            item = self.params_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        
        # Recuperer l'action selectionnee
        action_id = self.combo_action.currentData()
        action_def = ACTIONS.get(action_id, {})
        
        # Mettre a jour la description
        self.lbl_desc.setText(action_def.get("description", ""))
        
        # Creer les champs parametres
        params = action_def.get("params", {})
        for param_id, param_def in params.items():
            # Label du parametre
            label_text = param_def["label"]
            if param_def.get("required"):
                label_text += " *"
            lbl = QLabel(label_text)
            lbl.setStyleSheet("font-size: 11px; font-weight: bold; color: #495057; background: transparent;")
            lbl.setFixedWidth(120)
            self.params_layout.addWidget(lbl)
            
            # Widget selon le type
            param_type = param_def.get("type", "text")
            
            if param_type == "choice":
                widget = QComboBox()
                choices = param_def.get("choices", [])
                for c in choices:
                    widget.addItem(c)
                widget.setMinimumWidth(140)
            elif param_type == "bool":
                widget = QComboBox()
                widget.addItem("Non", False)
                widget.addItem("Oui", True)
                widget.setFixedWidth(80)
            elif param_type == "number":
                widget = QLineEdit()
                widget.setPlaceholderText(param_def.get("placeholder", ""))
                widget.setFixedWidth(80)
            else:
                widget = QLineEdit()
                widget.setPlaceholderText(param_def.get("placeholder", ""))
                widget.setMinimumWidth(200)
            
            widget.setStyleSheet("padding: 3px 6px; font-size: 12px;")
            self.params_layout.addWidget(widget, 1 if param_type == "text" else 0)
            self.param_widgets[param_id] = widget
        
        # Spacer a droite
        self.params_layout.addStretch()
    
    def set_number(self, num):
        """Met a jour le numero d'etape"""
        self.step_number = num
        self.lbl_number.setText(f"#{num}")
    
    def get_data(self):
        """Retourne les donnees de l'etape en dict"""
        action_id = self.combo_action.currentData()
        data = {"action": action_id}
        
        for param_id, widget in self.param_widgets.items():
            if isinstance(widget, QComboBox):
                if widget.currentData() is not None:
                    data[param_id] = widget.currentData()
                else:
                    data[param_id] = widget.currentText()
            elif isinstance(widget, QLineEdit):
                val = widget.text().strip()
                if val:
                    data[param_id] = val
        
        return data
    
    def set_data(self, data):
        """Charge les donnees dans l'etape"""
        # Selectionner l'action
        action_id = data.get("action", "click")
        for i in range(self.combo_action.count()):
            if self.combo_action.itemData(i) == action_id:
                self.combo_action.setCurrentIndex(i)
                break
        
        # Remplir les parametres
        for param_id, value in data.items():
            if param_id == "action":
                continue
            widget = self.param_widgets.get(param_id)
            if widget:
                if isinstance(widget, QComboBox):
                    idx = widget.findText(str(value))
                    if idx >= 0:
                        widget.setCurrentIndex(idx)
                elif isinstance(widget, QLineEdit):
                    widget.setText(str(value))


# ============================================================
# FENETRE PRINCIPALE
# ============================================================

class ScenarioBuilder(QMainWindow):
    """Fenetre principale du constructeur de scenarios"""
    
    def __init__(self):
        super().__init__()
        self.steps = []
        self._setup_ui()
        self._add_step()  # Commencer avec une etape vide
    
    def _setup_ui(self):
        """Construit l'interface principale"""
        self.setWindowTitle("QA Scenario Builder — JMer Consulting")
        self.setMinimumSize(900, 600)
        self.resize(1100, 700)
        
        # Widget central
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        
        # --- En-tete : Nom du scenario + URL ---
        header_group = QGroupBox("Scenario")
        header_group.setStyleSheet("""
            QGroupBox { 
                font-weight: bold; font-size: 13px; 
                border: 1px solid #ced4da; border-radius: 6px; 
                margin-top: 8px; padding-top: 16px; 
            }
            QGroupBox::title { 
                subcontrol-origin: margin; left: 12px; padding: 0 6px; 
            }
        """)
        header_layout = QHBoxLayout(header_group)
        header_layout.setSpacing(10)
        
        header_layout.addWidget(QLabel("Nom :"))
        self.txt_name = QLineEdit()
        self.txt_name.setPlaceholderText("Parcours achat client, Recherche joueur...")
        self.txt_name.setMinimumWidth(250)
        header_layout.addWidget(self.txt_name, 1)
        
        header_layout.addWidget(QLabel("URL de depart :"))
        self.txt_url = QLineEdit()
        self.txt_url.setPlaceholderText("https://www.example.com")
        self.txt_url.setMinimumWidth(350)
        header_layout.addWidget(self.txt_url, 2)
        
        layout.addWidget(header_group)
        
        # --- Zone des etapes (scrollable) ---
        steps_group = QGroupBox("Etapes du scenario")
        steps_group.setStyleSheet("""
            QGroupBox { 
                font-weight: bold; font-size: 13px; 
                border: 1px solid #ced4da; border-radius: 6px; 
                margin-top: 8px; padding-top: 16px; 
            }
            QGroupBox::title { 
                subcontrol-origin: margin; left: 12px; padding: 0 6px; 
            }
        """)
        steps_outer = QVBoxLayout(steps_group)
        
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: none; }")
        
        self.steps_widget = QWidget()
        self.steps_layout = QVBoxLayout(self.steps_widget)
        self.steps_layout.setContentsMargins(4, 4, 4, 4)
        self.steps_layout.setSpacing(4)
        self.steps_layout.addStretch()
        
        scroll.setWidget(self.steps_widget)
        steps_outer.addWidget(scroll)
        
        layout.addWidget(steps_group, 1)
        
        # --- Boutons d'action ---
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(8)
        
        self.btn_add = QPushButton("+ Ajouter etape")
        self.btn_add.setStyleSheet("""
            QPushButton { 
                background-color: #0d6efd; color: white; 
                padding: 8px 20px; border-radius: 4px; 
                font-weight: bold; font-size: 12px; 
            }
            QPushButton:hover { background-color: #0b5ed7; }
        """)
        self.btn_add.clicked.connect(self._add_step)
        btn_layout.addWidget(self.btn_add)
        
        btn_layout.addStretch()
        
        # Compteur d'etapes
        self.lbl_count = QLabel("0 etapes")
        self.lbl_count.setStyleSheet("color: #6c757d; font-size: 12px;")
        btn_layout.addWidget(self.lbl_count)
        
        btn_layout.addStretch()
        
        self.btn_preview = QPushButton("Apercu JSON")
        self.btn_preview.setStyleSheet("""
            QPushButton { 
                background-color: #6c757d; color: white; 
                padding: 8px 16px; border-radius: 4px; font-size: 12px; 
            }
            QPushButton:hover { background-color: #5c636a; }
        """)
        self.btn_preview.clicked.connect(self._preview_json)
        btn_layout.addWidget(self.btn_preview)
        
        self.btn_load = QPushButton("Charger JSON")
        self.btn_load.setStyleSheet("""
            QPushButton { 
                background-color: #198754; color: white; 
                padding: 8px 16px; border-radius: 4px; font-size: 12px; 
            }
            QPushButton:hover { background-color: #157347; }
        """)
        self.btn_load.clicked.connect(self._load_json)
        btn_layout.addWidget(self.btn_load)
        
        self.btn_save = QPushButton("Sauvegarder JSON")
        self.btn_save.setStyleSheet("""
            QPushButton { 
                background-color: #198754; color: white; 
                padding: 8px 16px; border-radius: 4px; 
                font-weight: bold; font-size: 12px; 
            }
            QPushButton:hover { background-color: #157347; }
        """)
        self.btn_save.clicked.connect(self._save_json)
        btn_layout.addWidget(self.btn_save)
        
        self.btn_run = QPushButton("Lancer QA Explorer")
        self.btn_run.setStyleSheet("""
            QPushButton { 
                background-color: #dc3545; color: white; 
                padding: 8px 20px; border-radius: 4px; 
                font-weight: bold; font-size: 13px; 
            }
            QPushButton:hover { background-color: #bb2d3b; }
        """)
        self.btn_run.clicked.connect(self._run_explorer)
        btn_layout.addWidget(self.btn_run)
        
        layout.addLayout(btn_layout)
        
        # Barre de statut
        self.statusBar().showMessage("Pret — Ajoutez des etapes a votre scenario")
    
    # --- GESTION DES ETAPES ---
    
    def _add_step(self, data=None):
        """Ajoute une nouvelle etape"""
        step = StepWidget(step_number=len(self.steps) + 1)
        step.delete_requested.connect(self._delete_step)
        step.move_up_requested.connect(self._move_step_up)
        step.move_down_requested.connect(self._move_step_down)
        
        if data:
            step.set_data(data)
        
        self.steps.append(step)
        # Inserer avant le stretch
        self.steps_layout.insertWidget(self.steps_layout.count() - 1, step)
        self._update_numbers()
        self.statusBar().showMessage(f"Etape #{len(self.steps)} ajoutee")
    
    def _delete_step(self, step_widget):
        """Supprime une etape"""
        if len(self.steps) <= 1:
            QMessageBox.warning(self, "Attention", "Il faut au moins une etape dans le scenario.")
            return
        
        self.steps.remove(step_widget)
        self.steps_layout.removeWidget(step_widget)
        step_widget.deleteLater()
        self._update_numbers()
        self.statusBar().showMessage(f"Etape supprimee — {len(self.steps)} etapes restantes")
    
    def _move_step_up(self, step_widget):
        """Monte une etape d'un cran"""
        idx = self.steps.index(step_widget)
        if idx == 0:
            return
        self.steps[idx], self.steps[idx - 1] = self.steps[idx - 1], self.steps[idx]
        self._rebuild_steps_layout()
    
    def _move_step_down(self, step_widget):
        """Descend une etape d'un cran"""
        idx = self.steps.index(step_widget)
        if idx >= len(self.steps) - 1:
            return
        self.steps[idx], self.steps[idx + 1] = self.steps[idx + 1], self.steps[idx]
        self._rebuild_steps_layout()
    
    def _rebuild_steps_layout(self):
        """Reconstruit le layout des etapes apres reordonnancement"""
        # Retirer tous les widgets du layout (sans les detruire)
        while self.steps_layout.count():
            self.steps_layout.takeAt(0)
        
        # Remettre dans l'ordre
        for step in self.steps:
            self.steps_layout.addWidget(step)
        self.steps_layout.addStretch()
        self._update_numbers()
    
    def _update_numbers(self):
        """Met a jour la numerotation des etapes"""
        for i, step in enumerate(self.steps):
            step.set_number(i + 1)
        self.lbl_count.setText(f"{len(self.steps)} etape{'s' if len(self.steps) > 1 else ''}")
    
    # --- EXPORT / IMPORT JSON ---
    
    def _build_scenario(self):
        """Construit le dict du scenario"""
        name = self.txt_name.text().strip() or "Scenario sans nom"
        url = self.txt_url.text().strip()
        
        if not url:
            QMessageBox.warning(self, "URL manquante", "L'URL de depart est obligatoire.")
            return None
        
        steps_data = []
        for step in self.steps:
            data = step.get_data()
            # Verifier les parametres requis
            action_def = ACTIONS.get(data["action"], {})
            for param_id, param_def in action_def.get("params", {}).items():
                if param_def.get("required") and param_id not in data:
                    QMessageBox.warning(
                        self, "Parametre manquant",
                        f"Etape #{step.step_number} ({action_def['label']}) : "
                        f"le parametre '{param_def['label']}' est obligatoire."
                    )
                    return None
            steps_data.append(data)
        
        return {
            "name": name,
            "url": url,
            "created": datetime.now().isoformat(),
            "version": "1.0",
            "steps": steps_data
        }
    
    def _save_json(self):
        """Sauvegarde le scenario en JSON"""
        scenario = self._build_scenario()
        if not scenario:
            return
        
        # Nom de fichier par defaut base sur le nom du scenario
        default_name = scenario["name"].lower().replace(" ", "_").replace("'", "")
        default_name = "".join(c for c in default_name if c.isalnum() or c == "_")
        
        filepath, _ = QFileDialog.getSaveFileName(
            self, "Sauvegarder le scenario",
            f"scenario_{default_name}.json",
            "JSON (*.json)"
        )
        
        if filepath:
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(scenario, f, indent=2, ensure_ascii=False)
            self.statusBar().showMessage(f"Scenario sauvegarde : {filepath}")
    
    def _load_json(self):
        """Charge un scenario depuis un fichier JSON"""
        filepath, _ = QFileDialog.getOpenFileName(
            self, "Charger un scenario",
            "", "JSON (*.json)"
        )
        
        if not filepath:
            return
        
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                scenario = json.load(f)
        except Exception as e:
            QMessageBox.critical(self, "Erreur", f"Impossible de lire le fichier :\n{e}")
            return
        
        # Vider les etapes existantes
        for step in self.steps[:]:
            self.steps_layout.removeWidget(step)
            step.deleteLater()
        self.steps.clear()
        
        # Charger les donnees
        self.txt_name.setText(scenario.get("name", ""))
        self.txt_url.setText(scenario.get("url", ""))
        
        for step_data in scenario.get("steps", []):
            self._add_step(data=step_data)
        
        self.statusBar().showMessage(f"Scenario charge : {filepath} ({len(self.steps)} etapes)")
    
    def _preview_json(self):
        """Affiche un apercu du JSON genere"""
        scenario = self._build_scenario()
        if not scenario:
            return
        
        # Fenetre d'apercu
        preview = QMessageBox(self)
        preview.setWindowTitle("Apercu JSON")
        preview.setIcon(QMessageBox.Information)
        preview.setText(f"Scenario : {scenario['name']}\n{len(scenario['steps'])} etapes")
        preview.setDetailedText(json.dumps(scenario, indent=2, ensure_ascii=False))
        preview.exec()
    
    def _run_explorer(self):
        """Lance qa_explorer.py avec le scenario"""
        scenario = self._build_scenario()
        if not scenario:
            return
        
        # Sauvegarder temporairement
        temp_path = os.path.join(os.path.dirname(__file__), "_temp_scenario.json")
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(scenario, f, indent=2, ensure_ascii=False)
        
        # Lancer le script
        import subprocess
        explorer_path = os.path.join(os.path.dirname(__file__), "qa_explorer.py")
        
        if not os.path.exists(explorer_path):
            QMessageBox.warning(
                self, "qa_explorer.py introuvable",
                f"Le fichier qa_explorer.py n'a pas ete trouve dans :\n{os.path.dirname(__file__)}\n\n"
                f"Le scenario a ete sauvegarde dans :\n{temp_path}"
            )
            return
        
        self.statusBar().showMessage("Lancement de QA Explorer...")
        try:
            subprocess.Popen(
                [sys.executable, explorer_path, temp_path],
                creationflags=subprocess.CREATE_NEW_CONSOLE if sys.platform == "win32" else 0
            )
            self.statusBar().showMessage("QA Explorer lance !")
        except Exception as e:
            QMessageBox.critical(self, "Erreur", f"Impossible de lancer QA Explorer :\n{e}")


# ============================================================
# POINT D'ENTREE
# ============================================================

if __name__ == "__main__":
    app = QApplication(sys.argv)
    
    # Style global
    app.setStyle("Fusion")
    app.setFont(QFont("Segoe UI", 10))
    
    window = ScenarioBuilder()
    window.show()
    sys.exit(app.exec())
