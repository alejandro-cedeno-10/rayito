"""Reglas de detección de proyectos de agente (`agent-detection.json`).

GENERADO por `scripts/gen_agent_detection.py` desde `agent-detection.json`
(raíz del repo): no editar a mano, ejecutar `make agent-detection`. Los
esquemas y las reglas son los mismos que usa el SDK de TypeScript
(`agent/detection-rules.gen.ts`).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Final

_RULES_JSON: Final = r"""
{
 "schema": "rayito.agent-detection-rules/1",
 "comment": "Reglas de detección de `rayito agent deploy` (fuente única de los dos SDK). Genera `_detection_rules.py` y `detection-rules.gen.ts` con `scripts/gen_agent_detection.py`; los vectores compartidos viven en `testdata/agent-detection/`. Las expresiones regulares usan el subconjunto común de Python y JavaScript (sin lookaround ni grupos con nombre), se evalúan con el modo multilínea y sin Unicode.",
 "ignoredPathSegments": [
  "tests",
  "test",
  "__tests__",
  "examples",
  "docs",
  "scripts",
  "migrations"
 ],
 "envExampleFiles": [
  ".env.example",
  ".env.sample",
  ".env.template"
 ],
 "envScanFiles": [
  "README.md"
 ],
 "httpEntryKeyword": "http",
 "preferNames": [
  "agent",
  "graph",
  "app",
  "workflow",
  "harness",
  "build_graph",
  "build_agent",
  "make_agent",
  "make_graph",
  "create_graph",
  "get_agent",
  "get_graph",
  "buildAgent",
  "makeAgent",
  "createHarness",
  "buildHarness",
  "getAgent"
 ],
 "preferFiles": [
  "agent",
  "graph",
  "main",
  "app",
  "server",
  "index",
  "workflow"
 ],
 "maxCandidatesListed": 5,
 "languages": {
  "python": {
   "markers": [
    "pyproject.toml",
    "requirements*.txt",
    "setup.py",
    "uv.lock",
    "poetry.lock",
    "Pipfile.lock"
   ],
   "sources": [
    "*.py"
   ],
   "sourceExcludes": [],
   "dependencyFiles": [
    {
     "glob": "requirements*.txt",
     "format": "requirements"
    },
    {
     "glob": "pyproject.toml",
     "format": "pyproject"
    }
   ]
  },
  "typescript": {
   "markers": [
    "package.json",
    "tsconfig.json"
   ],
   "sources": [
    "*.ts",
    "*.mts",
    "*.js",
    "*.mjs"
   ],
   "sourceExcludes": [
    "*.d.ts"
   ],
   "dependencyFiles": [
    {
     "glob": "package.json",
     "format": "package-json"
    }
   ]
  }
 },
 "entryFiles": {
  "langgraph": {
   "file": "langgraph.json",
   "key": "graphs",
   "preferKeys": [
    "agent"
   ],
   "languagesBySuffix": {
    ".py": "python",
    ".ts": "typescript",
    ".mts": "typescript",
    ".js": "typescript",
    ".mjs": "typescript"
   }
  }
 },
 "frameworks": [
  {
   "id": "langgraph-json",
   "framework": "langgraph",
   "kind": {
    "python": "langgraph",
    "typescript": "langgraph"
   },
   "agnosticFiles": [
    "langgraph.json"
   ],
   "entryFile": "langgraph"
  },
  {
   "id": "strands-harness",
   "framework": "strands-harness",
   "kind": {
    "python": "strands-harness",
    "typescript": "strands"
   },
   "deps": {
    "python": [
     "strands-harness"
    ],
    "typescript": [
     "@strands-agents/harness"
    ]
   },
   "entries": {
    "python": [
     {
      "id": "py-create-harness",
      "pattern": "^(\\w+)\\s*(?::[^=\\n]+)?=\\s*create_harness\\(",
      "tier": 1,
      "factory": false
     },
     {
      "id": "py-factory-noargs",
      "pattern": "^(?:async\\s+)?def\\s+((?:build|make|create|get|new)_\\w*(?:agent|graph|harness|workflow)\\w*)\\(\\s*\\)",
      "tier": 2,
      "factory": true,
      "requires": "create_harness\\("
     },
     {
      "id": "py-factory-args",
      "pattern": "^(?:async\\s+)?def\\s+((?:build|make|create|get|new)_\\w*(?:agent|graph|harness|workflow)\\w*)\\(\\s*[^)\\s][^)]*\\)",
      "tier": 3,
      "factory": true,
      "requires": "create_harness\\("
     }
    ],
    "typescript": [
     {
      "id": "ts-create-harness",
      "pattern": "^export\\s+const\\s+(\\w+)\\s*(?::[^=\\n]+)?=\\s*(?:await\\s+)?createHarness\\(",
      "tier": 1,
      "factory": false
     },
     {
      "id": "ts-default-harness",
      "pattern": "^export\\s+default\\s+createHarness\\(",
      "tier": 1,
      "factory": false,
      "name": "default"
     },
     {
      "id": "ts-factory-noargs",
      "pattern": "^export\\s+(?:async\\s+)?function\\s+((?:build|make|create|get|new)\\w*(?:Agent|Graph|Harness|Workflow)\\w*)\\(\\s*\\)",
      "tier": 2,
      "factory": true,
      "requires": "createHarness\\("
     },
     {
      "id": "ts-factory-args",
      "pattern": "^export\\s+(?:async\\s+)?function\\s+((?:build|make|create|get|new)\\w*(?:Agent|Graph|Harness|Workflow)\\w*)\\(\\s*[^)\\s][^)]*\\)",
      "tier": 3,
      "factory": true,
      "requires": "createHarness\\("
     },
     {
      "id": "ts-factory-arrow",
      "pattern": "^export\\s+const\\s+((?:build|make|create|get|new)\\w*(?:Agent|Graph|Harness|Workflow)\\w*)\\s*=\\s*(?:async\\s*)?\\(\\s*\\)\\s*(?::[^=\\n]+)?=>",
      "tier": 2,
      "factory": true,
      "requires": "createHarness\\("
     }
    ]
   }
  },
  {
   "id": "strands",
   "framework": "strands",
   "kind": {
    "python": "strands",
    "typescript": "strands"
   },
   "deps": {
    "python": [
     "strands-agents"
    ],
    "typescript": [
     "@strands-agents/sdk"
    ]
   },
   "entries": {
    "python": [
     {
      "id": "py-strands-agent",
      "pattern": "^(\\w+)\\s*(?::[^=\\n]+)?=\\s*Agent\\(",
      "tier": 1,
      "factory": false,
      "requires": "^(?:from|import)\\s+strands\\b"
     },
     {
      "id": "py-factory-noargs",
      "pattern": "^(?:async\\s+)?def\\s+((?:build|make|create|get|new)_\\w*(?:agent|graph|harness|workflow)\\w*)\\(\\s*\\)",
      "tier": 2,
      "factory": true,
      "requires": "^(?:from|import)\\s+strands\\b"
     },
     {
      "id": "py-factory-args",
      "pattern": "^(?:async\\s+)?def\\s+((?:build|make|create|get|new)_\\w*(?:agent|graph|harness|workflow)\\w*)\\(\\s*[^)\\s][^)]*\\)",
      "tier": 3,
      "factory": true,
      "requires": "^(?:from|import)\\s+strands\\b"
     }
    ],
    "typescript": [
     {
      "id": "ts-strands-agent",
      "pattern": "^export\\s+const\\s+(\\w+)\\s*(?::[^=\\n]+)?=\\s*new\\s+Agent\\(",
      "tier": 1,
      "factory": false,
      "requires": "@strands-agents/sdk"
     },
     {
      "id": "ts-factory-noargs",
      "pattern": "^export\\s+(?:async\\s+)?function\\s+((?:build|make|create|get|new)\\w*(?:Agent|Graph|Harness|Workflow)\\w*)\\(\\s*\\)",
      "tier": 2,
      "factory": true,
      "requires": "@strands-agents/sdk"
     },
     {
      "id": "ts-factory-args",
      "pattern": "^export\\s+(?:async\\s+)?function\\s+((?:build|make|create|get|new)\\w*(?:Agent|Graph|Harness|Workflow)\\w*)\\(\\s*[^)\\s][^)]*\\)",
      "tier": 3,
      "factory": true,
      "requires": "@strands-agents/sdk"
     },
     {
      "id": "ts-factory-arrow",
      "pattern": "^export\\s+const\\s+((?:build|make|create|get|new)\\w*(?:Agent|Graph|Harness|Workflow)\\w*)\\s*=\\s*(?:async\\s*)?\\(\\s*\\)\\s*(?::[^=\\n]+)?=>",
      "tier": 2,
      "factory": true,
      "requires": "@strands-agents/sdk"
     }
    ]
   }
  },
  {
   "id": "deepagents",
   "framework": "deepagents",
   "kind": {
    "python": "langgraph",
    "typescript": "langgraph"
   },
   "deps": {
    "python": [
     "deepagents"
    ],
    "typescript": [
     "deepagents"
    ]
   },
   "entries": {
    "python": [
     {
      "id": "py-create-deep-agent",
      "pattern": "^(\\w+)\\s*(?::[^=\\n]+)?=\\s*create_deep_agent\\(",
      "tier": 1,
      "factory": false
     },
     {
      "id": "py-factory-noargs",
      "pattern": "^(?:async\\s+)?def\\s+((?:build|make|create|get|new)_\\w*(?:agent|graph|harness|workflow)\\w*)\\(\\s*\\)",
      "tier": 2,
      "factory": true,
      "requires": "create_deep_agent\\("
     },
     {
      "id": "py-factory-args",
      "pattern": "^(?:async\\s+)?def\\s+((?:build|make|create|get|new)_\\w*(?:agent|graph|harness|workflow)\\w*)\\(\\s*[^)\\s][^)]*\\)",
      "tier": 3,
      "factory": true,
      "requires": "create_deep_agent\\("
     }
    ],
    "typescript": [
     {
      "id": "ts-create-deep-agent",
      "pattern": "^export\\s+const\\s+(\\w+)\\s*(?::[^=\\n]+)?=\\s*createDeepAgent\\(",
      "tier": 1,
      "factory": false
     },
     {
      "id": "ts-factory-noargs",
      "pattern": "^export\\s+(?:async\\s+)?function\\s+((?:build|make|create|get|new)\\w*(?:Agent|Graph|Harness|Workflow)\\w*)\\(\\s*\\)",
      "tier": 2,
      "factory": true,
      "requires": "createDeepAgent\\("
     },
     {
      "id": "ts-factory-args",
      "pattern": "^export\\s+(?:async\\s+)?function\\s+((?:build|make|create|get|new)\\w*(?:Agent|Graph|Harness|Workflow)\\w*)\\(\\s*[^)\\s][^)]*\\)",
      "tier": 3,
      "factory": true,
      "requires": "createDeepAgent\\("
     },
     {
      "id": "ts-factory-arrow",
      "pattern": "^export\\s+const\\s+((?:build|make|create|get|new)\\w*(?:Agent|Graph|Harness|Workflow)\\w*)\\s*=\\s*(?:async\\s*)?\\(\\s*\\)\\s*(?::[^=\\n]+)?=>",
      "tier": 2,
      "factory": true,
      "requires": "createDeepAgent\\("
     }
    ]
   }
  },
  {
   "id": "langgraph",
   "framework": "langgraph",
   "kind": {
    "python": "langgraph",
    "typescript": "langgraph"
   },
   "deps": {
    "python": [
     "langgraph",
     "langchain"
    ],
    "typescript": [
     "@langchain/langgraph",
     "langchain"
    ]
   },
   "entries": {
    "python": [
     {
      "id": "py-create-react-agent",
      "pattern": "^(\\w+)\\s*(?::[^=\\n]+)?=\\s*create_react_agent\\(",
      "tier": 1,
      "factory": false
     },
     {
      "id": "py-create-agent",
      "pattern": "^(\\w+)\\s*(?::[^=\\n]+)?=\\s*create_agent\\(",
      "tier": 1,
      "factory": false
     },
     {
      "id": "py-compile",
      "pattern": "^(\\w+)\\s*(?::[^=\\n]+)?=\\s*[\\w.]+\\.compile\\(",
      "tier": 1,
      "factory": false
     },
     {
      "id": "py-factory-noargs",
      "pattern": "^(?:async\\s+)?def\\s+((?:build|make|create|get|new)_\\w*(?:agent|graph|harness|workflow)\\w*)\\(\\s*\\)",
      "tier": 2,
      "factory": true,
      "requires": "create_react_agent\\(|create_agent\\(|\\.compile\\(|StateGraph\\("
     },
     {
      "id": "py-factory-args",
      "pattern": "^(?:async\\s+)?def\\s+((?:build|make|create|get|new)_\\w*(?:agent|graph|harness|workflow)\\w*)\\(\\s*[^)\\s][^)]*\\)",
      "tier": 3,
      "factory": true,
      "requires": "create_react_agent\\(|create_agent\\(|\\.compile\\(|StateGraph\\("
     }
    ],
    "typescript": [
     {
      "id": "ts-create-react-agent",
      "pattern": "^export\\s+const\\s+(\\w+)\\s*(?::[^=\\n]+)?=\\s*createReactAgent\\(",
      "tier": 1,
      "factory": false
     },
     {
      "id": "ts-create-agent",
      "pattern": "^export\\s+const\\s+(\\w+)\\s*(?::[^=\\n]+)?=\\s*createAgent\\(",
      "tier": 1,
      "factory": false
     },
     {
      "id": "ts-compile",
      "pattern": "^export\\s+const\\s+(\\w+)\\s*(?::[^=\\n]+)?=\\s*[\\w.]+\\.compile\\(",
      "tier": 1,
      "factory": false
     },
     {
      "id": "ts-factory-noargs",
      "pattern": "^export\\s+(?:async\\s+)?function\\s+((?:build|make|create|get|new)\\w*(?:Agent|Graph|Harness|Workflow)\\w*)\\(\\s*\\)",
      "tier": 2,
      "factory": true,
      "requires": "createReactAgent\\(|createAgent\\(|\\.compile\\(|StateGraph\\("
     },
     {
      "id": "ts-factory-args",
      "pattern": "^export\\s+(?:async\\s+)?function\\s+((?:build|make|create|get|new)\\w*(?:Agent|Graph|Harness|Workflow)\\w*)\\(\\s*[^)\\s][^)]*\\)",
      "tier": 3,
      "factory": true,
      "requires": "createReactAgent\\(|createAgent\\(|\\.compile\\(|StateGraph\\("
     },
     {
      "id": "ts-factory-arrow",
      "pattern": "^export\\s+const\\s+((?:build|make|create|get|new)\\w*(?:Agent|Graph|Harness|Workflow)\\w*)\\s*=\\s*(?:async\\s*)?\\(\\s*\\)\\s*(?::[^=\\n]+)?=>",
      "tier": 2,
      "factory": true,
      "requires": "createReactAgent\\(|createAgent\\(|\\.compile\\(|StateGraph\\("
     }
    ]
   }
  },
  {
   "id": "vercel-ai",
   "framework": "vercel-ai",
   "kind": {
    "typescript": "custom"
   },
   "deps": {
    "typescript": [
     "ai"
    ]
   },
   "entries": {
    "typescript": [
     {
      "id": "ts-tool-loop-agent",
      "pattern": "^export\\s+const\\s+(\\w+)\\s*(?::[^=\\n]+)?=\\s*new\\s+(?:ToolLoopAgent|Experimental_Agent|HarnessAgent)\\(",
      "tier": 1,
      "factory": false,
      "requires": "from\\s+[\\\"']ai[\\\"']"
     },
     {
      "id": "ts-factory-noargs",
      "pattern": "^export\\s+(?:async\\s+)?function\\s+((?:build|make|create|get|new)\\w*(?:Agent|Graph|Harness|Workflow)\\w*)\\(\\s*\\)",
      "tier": 2,
      "factory": true,
      "requires": "(?:ToolLoopAgent|Experimental_Agent|HarnessAgent)\\("
     },
     {
      "id": "ts-factory-args",
      "pattern": "^export\\s+(?:async\\s+)?function\\s+((?:build|make|create|get|new)\\w*(?:Agent|Graph|Harness|Workflow)\\w*)\\(\\s*[^)\\s][^)]*\\)",
      "tier": 3,
      "factory": true,
      "requires": "(?:ToolLoopAgent|Experimental_Agent|HarnessAgent)\\("
     },
     {
      "id": "ts-factory-arrow",
      "pattern": "^export\\s+const\\s+((?:build|make|create|get|new)\\w*(?:Agent|Graph|Harness|Workflow)\\w*)\\s*=\\s*(?:async\\s*)?\\(\\s*\\)\\s*(?::[^=\\n]+)?=>",
      "tier": 2,
      "factory": true,
      "requires": "(?:ToolLoopAgent|Experimental_Agent|HarnessAgent)\\("
     }
    ]
   }
  },
  {
   "id": "hermes",
   "framework": "hermes",
   "kind": {
    "python": "hermes",
    "typescript": "hermes"
   },
   "agnosticAllFiles": [
    "SOUL.md"
   ],
   "agnosticAnyFiles": [
    "config.yaml",
    "config.yml"
   ],
   "options": {
    "skills_dir": {
     "directory": "skills"
    }
   }
  },
  {
   "id": "opencode",
   "framework": "opencode",
   "kind": {
    "python": "opencode",
    "typescript": "opencode"
   },
   "agnosticFiles": [
    "opencode.json",
    "opencode.jsonc"
   ]
  },
  {
   "id": "agentcore",
   "framework": "agentcore",
   "webOnly": true,
   "kind": {
    "python": "custom"
   },
   "deps": {
    "python": [
     "bedrock-agentcore"
    ]
   },
   "entries": {
    "python": [
     {
      "id": "py-agentcore-app",
      "pattern": "^(\\w+)\\s*(?::[^=\\n]+)?=\\s*BedrockAgentCoreApp\\(",
      "tier": 1,
      "factory": false
     }
    ]
   },
   "app": {
    "launcher": "asgi",
    "fixedRoutes": [
     [
      "GET",
      "/ping"
     ],
     [
      "POST",
      "/invocations"
     ]
    ]
   }
  },
  {
   "id": "fastapi",
   "framework": "fastapi",
   "webOnly": true,
   "kind": {
    "python": "custom"
   },
   "deps": {
    "python": [
     "fastapi"
    ]
   },
   "entries": {
    "python": [
     {
      "id": "py-fastapi-app",
      "pattern": "^(\\w+)\\s*(?::[^=\\n]+)?=\\s*FastAPI\\(",
      "tier": 1,
      "factory": false
     }
    ]
   },
   "app": {
    "launcher": "asgi",
    "fixedRoutes": [
     [
      "GET",
      "/openapi.json"
     ]
    ]
   }
  },
  {
   "id": "starlette",
   "framework": "starlette",
   "webOnly": true,
   "kind": {
    "python": "custom"
   },
   "deps": {
    "python": [
     "starlette"
    ]
   },
   "entries": {
    "python": [
     {
      "id": "py-starlette-app",
      "pattern": "^(\\w+)\\s*(?::[^=\\n]+)?=\\s*Starlette\\(",
      "tier": 1,
      "factory": false
     }
    ]
   },
   "app": {
    "launcher": "asgi",
    "fixedRoutes": []
   }
  },
  {
   "id": "flask",
   "framework": "flask",
   "webOnly": true,
   "kind": {
    "python": "custom"
   },
   "deps": {
    "python": [
     "flask"
    ]
   },
   "entries": {
    "python": [
     {
      "id": "py-flask-app",
      "pattern": "^(\\w+)\\s*(?::[^=\\n]+)?=\\s*Flask\\(",
      "tier": 1,
      "factory": false
     }
    ]
   },
   "app": {
    "launcher": "flask",
    "fixedRoutes": []
   }
  },
  {
   "id": "express",
   "framework": "express",
   "webOnly": true,
   "kind": {
    "typescript": "custom"
   },
   "deps": {
    "typescript": [
     "express"
    ]
   },
   "entries": {
    "typescript": [
     {
      "id": "ts-express-app",
      "pattern": "^(?:export\\s+)?const\\s+(\\w+)\\s*(?::[^=\\n]+)?=\\s*express\\(\\)",
      "tier": 1,
      "factory": false
     }
    ]
   },
   "app": {
    "launcher": "node",
    "fixedRoutes": []
   }
  },
  {
   "id": "hono",
   "framework": "hono",
   "webOnly": true,
   "kind": {
    "typescript": "custom"
   },
   "deps": {
    "typescript": [
     "hono"
    ]
   },
   "entries": {
    "typescript": [
     {
      "id": "ts-hono-app",
      "pattern": "^(?:export\\s+)?const\\s+(\\w+)\\s*(?::[^=\\n]+)?=\\s*new\\s+Hono\\(",
      "tier": 1,
      "factory": false
     }
    ]
   },
   "app": {
    "launcher": "node",
    "fixedRoutes": []
   }
  },
  {
   "id": "fastify",
   "framework": "fastify",
   "webOnly": true,
   "kind": {
    "typescript": "custom"
   },
   "deps": {
    "typescript": [
     "fastify"
    ]
   },
   "entries": {
    "typescript": [
     {
      "id": "ts-fastify-app",
      "pattern": "^(?:export\\s+)?const\\s+(\\w+)\\s*(?::[^=\\n]+)?=\\s*(?:await\\s+)?[Ff]astify\\(",
      "tier": 1,
      "factory": false
     }
    ]
   },
   "app": {
    "launcher": "node",
    "fixedRoutes": []
   }
  }
 ],
 "app": {
  "launchers": {
   "asgi": {
    "command": [
     "python",
     "-m",
     "uvicorn",
     "{module}:{attr}",
     "--host",
     "127.0.0.1",
     "--port",
     "{port}"
    ],
    "portLiteral": false
   },
   "flask": {
    "command": [
     "python",
     "-m",
     "flask",
     "--app",
     "{module}:{attr}",
     "run",
     "--host",
     "127.0.0.1",
     "--port",
     "{port}"
    ],
    "portLiteral": false
   },
   "node": {
    "portLiteral": true,
    "startScript": "start"
   }
  },
  "routes": {
   "python": [
    {
     "pattern": "@\\w+\\.(get|post|put|delete|patch)\\(\\s*[\\\"']([^\\\"']+)[\\\"']",
     "methodGroup": 1,
     "pathGroup": 2
    },
    {
     "pattern": "@\\w+\\.route\\(\\s*[\\\"']([^\\\"']+)[\\\"'][^)\\n]*methods\\s*=\\s*\\[\\s*[\\\"'](\\w+)[\\\"']",
     "methodGroup": 2,
     "pathGroup": 1
    },
    {
     "pattern": "@\\w+\\.route\\(\\s*[\\\"']([^\\\"']+)[\\\"']\\s*\\)",
     "method": "GET",
     "pathGroup": 1
    }
   ],
   "typescript": [
    {
     "pattern": "\\b\\w+\\.(get|post|put|delete|patch)\\(\\s*[\\\"'`]([^\\\"'`\\n]+)[\\\"'`]",
     "methodGroup": 1,
     "pathGroup": 2
    }
   ]
  },
  "healthCandidates": [
   "/ping",
   "/health",
   "/healthz",
   "/healthcheck",
   "/health-check",
   "/api/health",
   "/status",
   "/ready",
   "/"
  ],
  "invokeCandidates": [
   "/invocations",
   "/invoke",
   "/chat",
   "/api/chat",
   "/ask",
   "/run",
   "/query",
   "/generate",
   "/message",
   "/completion",
   "/completions"
  ],
  "agentcorePaths": {
   "health": "/ping",
   "invoke": "/invocations"
  },
  "requestFields": [
   "prompt",
   "message",
   "input",
   "query",
   "question",
   "text"
  ],
  "requestFieldTemplates": [
   "[\"']{field}[\"']",
   "\\b{field}\\s*:\\s*str\\b",
   "\\b(?:body|request|req|payload|data)\\.{field}\\b"
  ],
  "responseFields": [
   "answer",
   "output",
   "text",
   "response",
   "result",
   "content"
  ],
  "responseFieldTemplates": [
   "[\"']{field}[\"']\\s*:"
  ],
  "portLiterals": {
   "python": [
    "\\bport\\s*=\\s*(\\d{2,5})"
   ],
   "typescript": [
    "\\.listen\\(\\s*(\\d{2,5})",
    "\\bport:\\s*(\\d{2,5})"
   ]
  },
  "portEnvTemplates": {
   "python": [
    "os\\.environ(?:\\.get)?[\\[(]\\s*[\"']{env}[\"']",
    "os\\.getenv\\(\\s*[\"']{env}[\"']"
   ],
   "typescript": [
    "process\\.env\\.{env}\\b",
    "process\\.env\\[\\s*[\"']{env}[\"']"
   ]
  },
  "reservedPorts": [
   8080,
   9000
  ]
 },
 "install": {
  "python": [
   {
    "file": "uv.lock",
    "manager": "uv",
    "reproducible": true
   },
   {
    "glob": "requirements*.txt",
    "manager": "pip",
    "reproducible": true,
    "preferred": [
     "requirements.txt"
    ],
    "ignored": [
     "*dev*",
     "*test*"
    ],
    "hashMarker": "--hash="
   },
   {
    "file": "poetry.lock",
    "manager": "poetry",
    "notDetected": true
   },
   {
    "file": "Pipfile.lock",
    "manager": "pipenv",
    "notDetected": true
   },
   {
    "file": "pyproject.toml",
    "manager": "pip-project",
    "reproducible": false
   }
  ],
  "typescript": [
   {
    "file": "package-lock.json",
    "manager": "npm",
    "reproducible": true
   },
   {
    "file": "pnpm-lock.yaml",
    "manager": "pnpm",
    "reproducible": true
   },
   {
    "file": "yarn.lock",
    "manager": "yarn",
    "reproducible": true,
    "berryMarker": "__metadata:",
    "berryManager": "yarn-berry"
   },
   {
    "file": "bun.lockb",
    "manager": "bun",
    "unsupported": true
   },
   {
    "file": "bun.lock",
    "manager": "bun",
    "unsupported": true
   },
   {
    "file": "package.json",
    "manager": "npm",
    "reproducible": false
   }
  ],
  "buildScript": "build"
 },
 "versions": {
  "python": {
   "sources": [
    {
     "file": "pyproject.toml",
     "pattern": "^\\s*requires-python\\s*=\\s*[\\\"']([^\\\"']+)[\\\"']",
     "kind": "specifier"
    },
    {
     "file": ".python-version",
     "pattern": "^\\s*(?:cpython-)?(\\d+\\.\\d+)",
     "kind": "minor"
    },
    {
     "file": "runtime.txt",
     "pattern": "^\\s*python-(\\d+\\.\\d+)",
     "kind": "minor"
    }
   ]
  },
  "typescript": {
   "sources": [
    {
     "file": "package.json",
     "json": [
      "engines",
      "node"
     ],
     "kind": "semver"
    },
    {
     "file": ".nvmrc",
     "pattern": "^\\s*v?(\\d+)",
     "kind": "major"
    },
    {
     "file": ".node-version",
     "pattern": "^\\s*v?(\\d+)",
     "kind": "major"
    }
   ]
  }
 },
 "providers": [
  {
   "preset": "bedrock",
   "deps": {
    "python": [
     "langchain-aws",
     "strands-agents[bedrock]",
     "boto3"
    ],
    "typescript": [
     "@langchain/aws",
     "@ai-sdk/amazon-bedrock",
     "@aws-sdk/client-bedrock-runtime"
    ]
   },
   "weakDeps": {
    "python": [
     "strands-agents",
     "strands-harness"
    ],
    "typescript": [
     "@strands-agents/sdk",
     "@strands-agents/harness"
    ]
   },
   "envs": [
    "AWS_BEARER_TOKEN_BEDROCK",
    "BEDROCK_MODEL_ID"
   ],
   "models": [
    "[\"'](?:[a-z][\\w-]*/)?((?:us|eu|apac|global)\\.(?:anthropic|amazon|meta|mistral)\\.[\\w.:-]+|(?:anthropic|amazon|meta|mistral)\\.[\\w.:-]*v\\d[\\w.:-]*)[\"']",
    "(?:default|model|model_id|modelId):\\s*((?:us|eu|apac|global)\\.(?:anthropic|amazon|meta|mistral)\\.[\\w.:-]+)"
   ]
  },
  {
   "preset": "anthropic",
   "deps": {
    "python": [
     "langchain-anthropic",
     "anthropic",
     "strands-agents[anthropic]"
    ],
    "typescript": [
     "@langchain/anthropic",
     "@ai-sdk/anthropic",
     "@anthropic-ai/sdk"
    ]
   },
   "envs": [
    "ANTHROPIC_API_KEY"
   ],
   "models": [
    "[\\\"'](?:[a-z_-]+:)?(claude-[\\w.-]+)[\\\"']"
   ]
  },
  {
   "preset": "openai",
   "deps": {
    "python": [
     "langchain-openai",
     "openai",
     "openai-agents",
     "strands-agents[openai]"
    ],
    "typescript": [
     "@langchain/openai",
     "@ai-sdk/openai",
     "openai",
     "@openai/agents"
    ]
   },
   "envs": [
    "OPENAI_API_KEY"
   ],
   "models": [
    "[\\\"'](?:[a-z_-]+:)?(gpt-[\\w.-]+|o[134](?:-[\\w-]+)?)[\\\"']"
   ]
  },
  {
   "preset": "gemini",
   "deps": {
    "python": [
     "langchain-google-genai",
     "google-genai",
     "google-generativeai",
     "google-adk",
     "strands-agents[gemini]"
    ],
    "typescript": [
     "@langchain/google-genai",
     "@ai-sdk/google",
     "@google/genai"
    ]
   },
   "envs": [
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY"
   ],
   "models": [
    "[\\\"'](?:[a-z_-]+:)?(gemini-[\\w.-]+)[\\\"']"
   ]
  },
  {
   "preset": "azure",
   "deps": {
    "python": [],
    "typescript": []
   },
   "envs": [
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_ENDPOINT"
   ]
  },
  {
   "preset": "openrouter",
   "deps": {
    "python": [
     "langchain-openrouter"
    ],
    "typescript": [
     "@openrouter/ai-sdk-provider"
    ]
   },
   "envs": [
    "OPENROUTER_API_KEY"
   ]
  },
  {
   "preset": "groq",
   "deps": {
    "python": [
     "langchain-groq",
     "groq"
    ],
    "typescript": [
     "@langchain/groq",
     "@ai-sdk/groq",
     "groq-sdk"
    ]
   },
   "envs": [
    "GROQ_API_KEY"
   ]
  },
  {
   "preset": "mistral",
   "deps": {
    "python": [
     "langchain-mistralai",
     "mistralai"
    ],
    "typescript": [
     "@langchain/mistralai",
     "@ai-sdk/mistral"
    ]
   },
   "envs": [
    "MISTRAL_API_KEY"
   ]
  },
  {
   "preset": "deepseek",
   "deps": {
    "python": [
     "langchain-deepseek"
    ],
    "typescript": [
     "@ai-sdk/deepseek"
    ]
   },
   "envs": [
    "DEEPSEEK_API_KEY"
   ]
  },
  {
   "preset": "xai",
   "deps": {
    "python": [
     "langchain-xai",
     "xai-sdk"
    ],
    "typescript": [
     "@ai-sdk/xai"
    ]
   },
   "envs": [
    "XAI_API_KEY"
   ]
  },
  {
   "preset": "litellm",
   "deps": {
    "python": [
     "litellm"
    ],
    "typescript": []
   },
   "envs": [
    "LITELLM_API_KEY"
   ]
  },
  {
   "preset": "openai-compatible",
   "deps": {
    "python": [],
    "typescript": [
     "@ai-sdk/openai-compatible"
    ]
   },
   "envs": []
  }
 ],
 "messages": {
  "ambiguous_language": "hay proyectos de Python y de TypeScript con el mismo peso; indica el lenguaje con --language",
  "ambiguous_entrypoint": "hay varios puntos de entrada posibles ({candidates}); indica uno con --entry o [runtime] entrypoint",
  "ambiguous_graph": "langgraph.json tiene varios grafos ({candidates}); elige uno con --graph o [runtime] graph",
  "ambiguous_factory": "la fábrica {candidates} necesita argumentos; indica el objeto o una fábrica sin argumentos con --entry o [runtime] entrypoint",
  "ambiguous_requirements": "hay varios ficheros de requisitos ({candidates}); indica uno con [source] requirements",
  "ambiguous_route": "la app tiene varias rutas POST posibles ({candidates}); indica la de invocación en [runtime.app] invoke_path",
  "not_detected_manifest": "no hay ningún proyecto de Python ni de TypeScript en la ruta (falta pyproject.toml, requirements.txt o package.json)",
  "not_detected_agent": "no se reconoce ningún framework de agente ni app HTTP; indica el framework con --framework y el punto de entrada con --entry",
  "not_detected_entrypoint": "se reconoce {framework} pero no se encuentra su punto de entrada; indícalo con --entry o [runtime] entrypoint",
  "graph_not_found": "langgraph.json no tiene el grafo {value} ({candidates})",
  "graph_conflict": "usa --graph o --entry, no los dos",
  "not_detected_poetry": "poetry.lock sin requirements.txt: exporta los requisitos (poetry export) o usa uv.lock",
  "not_detected_pipenv": "Pipfile.lock sin requirements.txt: exporta los requisitos (pipenv requirements) o usa uv.lock",
  "not_detected_start": "la app de Node no tiene script start ni main; indica el comando en [runtime.app] command",
  "unsupported_version": "ninguna versión fijada de {runtime} cumple la restricción del proyecto; usa una de las soportadas",
  "unsupported_port": "la app escucha en un puerto reservado ({port}); léelo de la variable PORT",
  "not_detected_route": "no se encontró una ruta POST para invocar la app; indícala en [runtime.app] invoke_path o usa el protocolo raw",
  "invalid_override": "{value} no es un valor válido para {option}; usa uno de {allowed}",
  "invalid_model_provider": "--model-provider {value} no es una pasarela conocida ({candidates})"
 },
 "warnings": {
  "scan_truncated": "el árbol del proyecto supera el límite de análisis; solo se leen los primeros ficheros",
  "file_unreadable": "no se pudo interpretar {file}; se ignora",
  "http_app_alternative": "el proyecto también tiene una app HTTP ({framework}); se sirve el framework del agente, y --entry http sirve la app",
  "entrypoint_not_found": "se reconoce {framework} pero no se encontró su punto de entrada; se prueba el siguiente",
  "multiple_lockfiles": "hay varios ficheros de bloqueo ({files}); se usa {used}",
  "not_reproducible": "sin fichero de bloqueo la instalación no es reproducible",
  "unsupported_lockfile": "{file} no está soportado; se instala sin él",
  "version_ignored": "la restricción de versión de {file} no se entiende y se ignora",
  "health_path_assumed": "la app no declara una ruta de salud; se usa {path}",
  "request_field_assumed": "no se detectó el campo de la petición; se usa {field}",
  "protocol_ignored": "el protocolo solo aplica a una app HTTP; se ignora",
  "several_providers": "hay varios proveedores de modelo ({providers}); se usa {used}",
  "no_provider": "no se detecta ningún proveedor de modelo; indícalo con --secret proveedor=nombre",
  "model_id_from_code": "no se fija el id del modelo: lo elige tu código ({envs})"
 },
 "maxModelsListed": 5,
 "modelFromCode": {
  "comment": "Modelo elegido por el código (ADR-033 D2). `clientBaseUrl`: un constructor de cliente con URL base convierte el proveedor `fromPreset` detectado por dependencia en `toPreset` (lo sustituye, no lo añade). Los nombres de variable salen de `envExampleFiles` (`envExampleName`), de las lecturas del código (`envReads`), de los campos de pydantic-settings (`settings`: `env_prefix` más el campo, en mayúsculas) y de las claves del mapa `env` de `langgraphJson`; se clasifican con `modelEnvPattern`, `baseUrlEnvPattern` y `apiKeyEnvPattern`. Nunca se lee un valor.",
  "clientBaseUrl": {
   "python": {
    "pattern": "\\b(?:ChatOpenAI|AzureChatOpenAI|OpenAI|AsyncOpenAI|OpenAIModel|init_chat_model)\\s*\\([\\s\\S]{0,400}?\\bbase_url\\s*="
   },
   "typescript": {
    "pattern": "\\b(?:ChatOpenAI|OpenAI|createOpenAI|createOpenAICompatible)\\s*\\(\\s*\\{[\\s\\S]{0,400}?\\b(?:baseURL|baseUrl|configuration)\\b"
   }
  },
  "fromPreset": "openai",
  "toPreset": "openai-compatible",
  "baseUrlRule": "code:base_url",
  "modelEnvPattern": {
   "pattern": "^(?:[A-Z][A-Z0-9]*_)*MODEL(?:_ID|_NAME)?$"
  },
  "baseUrlEnvPattern": {
   "pattern": "^(?:[A-Z][A-Z0-9]*_)*(?:BASE_URL|API_BASE)$"
  },
  "apiKeyEnvPattern": {
   "pattern": "^(?:[A-Z][A-Z0-9]*_)*API_KEY$"
  },
  "envExampleName": {
   "pattern": "^[ \\t]*(?:export[ \\t]+)?([A-Za-z_][A-Za-z0-9_]*)[ \\t]*="
  },
  "envReads": {
   "python": [
    {
     "pattern": "\\bos\\.(?:getenv|environ\\.get)\\(\\s*[\"']([A-Za-z_][A-Za-z0-9_]*)[\"']"
    },
    {
     "pattern": "\\bos\\.environ\\[\\s*[\"']([A-Za-z_][A-Za-z0-9_]*)[\"']"
    }
   ],
   "typescript": [
    {
     "pattern": "\\bprocess\\.env\\.([A-Za-z_][A-Za-z0-9_]*)"
    },
    {
     "pattern": "\\bprocess\\.env\\[\\s*[\"']([A-Za-z_][A-Za-z0-9_]*)[\"']"
    }
   ]
  },
  "settings": {
   "python": {
    "marker": {
     "pattern": "\\bBaseSettings\\b"
    },
    "prefix": {
     "pattern": "\\benv_prefix\\s*=\\s*[\"']([A-Za-z0-9_]*)[\"']"
    },
    "field": {
     "pattern": "^[ \\t]+([a-z_][a-z0-9_]*)[ \\t]*:[ \\t]*[A-Za-z]"
    }
   }
  },
  "langgraphJson": "langgraph.json"
 },
 "configProviders": [
  {
   "framework": "hermes",
   "files": [
    "config.yaml",
    "config.yml"
   ],
   "pattern": "^\\s*provider:\\s*[\\\"']?([A-Za-z0-9_-]+)",
   "aliases": {
    "amazon-bedrock": "bedrock",
    "google": "gemini",
    "azure-openai": "azure"
   }
  },
  {
   "framework": "opencode",
   "files": [
    "opencode.json",
    "opencode.jsonc"
   ],
   "pattern": "[\\\"']model[\\\"']\\s*:\\s*[\\\"']([A-Za-z0-9_-]+)/",
   "aliases": {
    "amazon-bedrock": "bedrock",
    "google": "gemini"
   }
  }
 ],
 "bridge": {
  "comment": "Del informe de detección al proyecto (`AgentProject.detect`): cómo se llama cada proveedor en el modelo, qué runtime sirve cada framework de JavaScript, qué tipos de runtime llevan las claves de instalación y cuáles esperan sólo la ruta del módulo.",
  "modelProviders": {
   "bedrock": "bedrock",
   "anthropic": "anthropic",
   "openai": "openai",
   "gemini": "google",
   "azure": "azure",
   "openrouter": "openai-compatible",
   "groq": "openai-compatible",
   "mistral": "openai-compatible",
   "deepseek": "openai-compatible",
   "xai": "openai-compatible",
   "litellm": "openai-compatible",
   "openai-compatible": "openai-compatible"
  },
  "nodeRuntimes": {
   "langgraph": {
    "kind": "custom",
    "adapter": "langgraph"
   },
   "deepagents": {
    "kind": "custom",
    "adapter": "langgraph"
   },
   "vercel-ai": {
    "kind": "custom",
    "adapter": "vercel-ai"
   }
  },
  "installKinds": [
   "langgraph",
   "strands",
   "strands-harness",
   "custom"
  ],
  "modulePathKinds": {
   "typescript": [
    "strands"
   ]
  },
  "defaultSource": ".",
  "defaultUvManifest": "pyproject.toml"
 }
}
"""

DETECTION_RULES: Final[Mapping[str, Any]] = json.loads(_RULES_JSON)
