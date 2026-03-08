# Machine-Learning-P3

## Getting Started

### Prerequisites

- **Python 3.8+** — [Download Python](https://www.python.org/downloads/)
- **pip** — comes with Python; verify with `pip --version`
- **Git** — to clone and manage the repository

### 1. Clone the Repository

```bash
git clone https://github.com/juanramirez260173-cmyk/Machine-Learning-P3.git
cd Machine-Learning-P3
```

### 2. Create a Virtual Environment

```bash
python -m venv venv
source venv/bin/activate    # Linux / macOS
# venv\Scripts\activate     # Windows
```

### 3. Install Dependencies

```bash
pip install -r requirements.txt
```

### 4. Project Structure

```
Machine-Learning-P3/
├── README.md
├── requirements.txt
├── notebooks/          # Jupyter notebooks for exploration
├── src/                # Source code and modules
│   ├── __init__.py
│   ├── data.py         # Data loading and preprocessing
│   ├── model.py        # Model definition and training
│   └── evaluate.py     # Evaluation and metrics
└── data/               # Datasets (not tracked by git)
```

### 5. Run

```bash
# Start Jupyter for interactive exploration
jupyter notebook

# Or run a script directly
python src/model.py
```

### Common Libraries

This project uses the following core libraries (listed in `requirements.txt`):

| Library | Purpose |
|---------|---------|
| numpy | Numerical computing |
| pandas | Data manipulation |
| scikit-learn | ML algorithms and utilities |
| matplotlib | Plotting and visualization |
| jupyter | Interactive notebooks |