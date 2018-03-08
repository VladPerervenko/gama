"""
Created on Thu Jan 11 11:45:30 2018
@author: Pieter Gijsbers
"""

import numpy as np
from sklearn.datasets import load_iris
from sklearn.model_selection import train_test_split
from sklearn.metrics import accuracy_score

from openml import tasks

from gama import Gama

if True:
    phoneme_task_id = 145857
    task = tasks.get_task(phoneme_task_id)
    X, y = task.get_X_and_y()
    X_train, X_test, y_train, y_test = train_test_split(X, y, stratify=y, shuffle=True, random_state=42)
else:
    iris = load_iris()
    X_train, X_test, y_train, y_test = train_test_split(iris.data, iris.target, stratify=iris.target, shuffle=True, random_state=42)

gpaml = Gama(random_state=1, n_generations = 10, pop_size=50)
gpaml.fit(X_train, y_train)
predictions_1 = gpaml.predict(X_test)
print('Accuracy n=1:', accuracy_score(y_test, predictions_1))
predictions_5 = gpaml.predict(X_test, auto_ensemble_n=5)
print('Accuracy n=5:', accuracy_score(y_test, predictions_5))

