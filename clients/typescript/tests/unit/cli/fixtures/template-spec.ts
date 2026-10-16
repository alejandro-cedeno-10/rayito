/** Un módulo de spec como el que `rayito template build` importa: exporta `template`. */

import { Template } from "../../../../src/templates/dsl.js";

export const template = new Template().fromBaseImage("rayito-base").pipInstall(["pandas"]);
