import joblib
bundle = joblib.load('/home/strata/strata/kaggle_and_strato_model_pruned.pkl')
print(bundle.get('thresholds'))
